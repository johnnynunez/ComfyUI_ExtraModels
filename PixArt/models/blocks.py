# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# --------------------------------------------------------
# References:
# GLIDE: https://github.com/openai/glide-text2im
# MAE: https://github.com/facebookresearch/mae/blob/main/models_mae.py
# --------------------------------------------------------
import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

import comfy.ldm.common_dit
from .utils import to_2tuple

sdpa_32b = None
Q_4GB_LIMIT = 32000000
"""If q is greater than this, the operation will likely require >4GB VRAM, which will fail on Intel Arc Alchemist GPUs without a workaround."""
# 2k   = 37 748 736
# 1024 =  9 437 184
# 2k model goes very slightly over 4GB

from comfy import model_management
if model_management.xformers_enabled():
    import xformers.ops
    if int((xformers.__version__).split(".")[2]) >= 28:
        block_diagonal_mask_from_seqlens = xformers.ops.fmha.attn_bias.BlockDiagonalMask.from_seqlens
    else:
        block_diagonal_mask_from_seqlens = xformers.ops.fmha.BlockDiagonalMask.from_seqlens
else:
    if model_management.xpu_available:
        import intel_extension_for_pytorch as ipex # type: ignore
        import os
        if not torch.xpu.has_fp64_dtype() and not os.environ.get('IPEX_FORCE_ATTENTION_SLICE', None):
            from ...utils.IPEX.attention import scaled_dot_product_attention_32_bit
            sdpa_32b = scaled_dot_product_attention_32_bit
            print("Using IPEX 4GB SDPA workaround")
        else:
            print("No IPEX 4GB workaround")

def modulate(x, shift, scale):
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)

def t2i_modulate(x, shift, scale):
    return x * (1 + scale) + shift

class MultiHeadCrossAttention(nn.Module):
    def __init__(self, d_model, num_heads, attn_drop=0., proj_drop=0., dtype=None, device=None, operations=None, **block_kwargs):
        super(MultiHeadCrossAttention, self).__init__()
        assert d_model % num_heads == 0, "d_model must be divisible by num_heads"

        self.d_model = d_model
        self.num_heads = num_heads
        self.head_dim = d_model // num_heads

        self.q_linear = operations.Linear(d_model, d_model, dtype=dtype, device=device)
        self.kv_linear = operations.Linear(d_model, d_model*2, dtype=dtype, device=device)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = operations.Linear(d_model, d_model, dtype=dtype, device=device)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x, cond, mask=None):
        # query/value: img tokens; key: condition; mask: if padding tokens
        B, N, C = x.shape

        q = self.q_linear(x).view(1, -1, self.num_heads, self.head_dim)
        kv = self.kv_linear(cond).view(1, -1, 2, self.num_heads, self.head_dim)
        k, v = kv.unbind(2)

        if model_management.xformers_enabled():
            attn_bias = None
            if mask is not None:
                attn_bias = block_diagonal_mask_from_seqlens([N] * B, mask)
            x = xformers.ops.memory_efficient_attention(
                q, k, v,
                p=self.attn_drop.p,
                attn_bias=attn_bias
            )
        else:
            q, k, v = map(lambda t: t.permute(0, 2, 1, 3),(q, k, v),)
            attn_mask = None
            if mask is not None and len(mask) > 1:

                # Create equivalent of xformer diagonal block mask, still only correct for square masks
                # But depth doesn't matter as tensors can expand in that dimension
                attn_mask_template = torch.ones(
                    [q.shape[2] // B, mask[0]],
                    dtype=torch.bool,
                    device=q.device
                )
                attn_mask = torch.block_diag(attn_mask_template)

                # create a mask on the diagonal for each mask in the batch
                for n in range(B - 1):
                    attn_mask = torch.block_diag(attn_mask, attn_mask_template)

            p = getattr(self.attn_drop, "p", 0) # IPEX.optimize() will turn attn_drop into an Identity()

            if sdpa_32b is not None and (q.element_size() * q.nelement()) > Q_4GB_LIMIT:
                sdpa = sdpa_32b
            else:
                sdpa = torch.nn.functional.scaled_dot_product_attention

            x = sdpa(
                q, k, v,
                attn_mask=attn_mask,
                dropout_p=p
            ).permute(0, 2, 1, 3).contiguous()
        x = x.view(B, -1, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class AttentionKVCompress(nn.Module):
    """Multi-head Attention block with KV token compression and qk norm."""

    def __init__(
        self,
        dim,
        num_heads=8,
        qkv_bias=True,
        sampling='conv',
        sr_ratio=1,
        qk_norm=False,
        dtype=None,
        device=None,
        operations=None,
        **block_kwargs,
    ):
        """
        Args:
            dim (int): Number of input channels.
            num_heads (int): Number of attention heads.
            qkv_bias (bool:  If True, add a learnable bias to query, key, value.
        """
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.scale = self.head_dim ** -0.5

        self.qkv = operations.Linear(dim, dim * 3, bias=qkv_bias, dtype=dtype, device=device)
        self.proj = operations.Linear(dim, dim, dtype=dtype, device=device)

        self.sampling=sampling    # ['conv', 'ave', 'uniform', 'uniform_every']
        self.sr_ratio = sr_ratio
        if sr_ratio > 1 and sampling == 'conv':
            # Avg Conv Init.
            self.sr = operations.Conv2d(dim, dim, groups=dim, kernel_size=sr_ratio, stride=sr_ratio, dtype=dtype, device=device)
            # self.sr.weight.data.fill_(1/sr_ratio**2)
            # self.sr.bias.data.zero_()
            self.norm = operations.LayerNorm(dim, dtype=dtype, device=device)
        if qk_norm:
            self.q_norm = operations.LayerNorm(dim, dtype=dtype, device=device)
            self.k_norm = operations.LayerNorm(dim, dtype=dtype, device=device)
        else:
            self.q_norm = nn.Identity()
            self.k_norm = nn.Identity()

    def downsample_2d(self, tensor, H, W, scale_factor, sampling=None):
        if sampling is None or scale_factor == 1:
            return tensor
        B, N, C = tensor.shape

        if sampling == 'uniform_every':
            return tensor[:, ::scale_factor], int(N // scale_factor)

        tensor = tensor.reshape(B, H, W, C).permute(0, 3, 1, 2)
        new_H, new_W = int(H / scale_factor), int(W / scale_factor)
        new_N = new_H * new_W

        if sampling == 'ave':
            tensor = F.interpolate(
                tensor, scale_factor=1 / scale_factor, mode='nearest'
            ).permute(0, 2, 3, 1)
        elif sampling == 'uniform':
            tensor = tensor[:, :, ::scale_factor, ::scale_factor].permute(0, 2, 3, 1)
        elif sampling == 'conv':
            tensor = self.sr(tensor).reshape(B, C, -1).permute(0, 2, 1)
            tensor = self.norm(tensor)
        else:
            raise ValueError

        return tensor.reshape(B, new_N, C).contiguous(), new_N

    def forward(self, x, mask=None, HW=None, block_id=None):
        B, N, C = x.shape # 2 4096 1152
        new_N = N
        if HW is None:
            H = W = int(N ** 0.5)
        else:
            H, W = HW
        qkv = self.qkv(x).reshape(B, N, 3, C)

        q, k, v = qkv.unbind(2)
        dtype = q.dtype
        q = self.q_norm(q)
        k = self.k_norm(k)

        # KV compression
        if self.sr_ratio > 1:
            k, new_N = self.downsample_2d(k, H, W, self.sr_ratio, sampling=self.sampling)
            v, new_N = self.downsample_2d(v, H, W, self.sr_ratio, sampling=self.sampling)

        q = q.reshape(B, N, self.num_heads, C // self.num_heads).to(dtype)
        k = k.reshape(B, new_N, self.num_heads, C // self.num_heads).to(dtype)
        v = v.reshape(B, new_N, self.num_heads, C // self.num_heads).to(dtype)

        attn_bias = None
        if mask is not None:
            attn_bias = torch.zeros([B * self.num_heads, q.shape[1], k.shape[1]], dtype=q.dtype, device=q.device)
            attn_bias.masked_fill_(mask.squeeze(1).repeat(self.num_heads, 1, 1) == 0, float('-inf'))
        # Switch between torch / xformers attention
        if model_management.xformers_enabled():
            x = xformers.ops.memory_efficient_attention(
                q, k, v,
                p=0,
                attn_bias=attn_bias
            )
        else:
            q, k, v = map(lambda t: t.transpose(1, 2),(q, k, v),)
            p = 0
            if sdpa_32b is not None and (q.element_size() * q.nelement()) > Q_4GB_LIMIT:
                sdpa = sdpa_32b
            else:
                sdpa = torch.nn.functional.scaled_dot_product_attention

            x = sdpa(
                q, k, v,
                dropout_p=p,
                attn_mask=attn_bias
            ).transpose(1, 2).contiguous()
        x = x.view(B, N, C)
        x = self.proj(x)
        return x


class FinalLayer(nn.Module):
    """
    The final layer of PixArt.
    """

    def __init__(self, hidden_size, patch_size, out_channels, dtype=None, device=None, operations=None):
        super().__init__()
        self.norm_final = operations.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        self.linear = operations.Linear(hidden_size, patch_size * patch_size * out_channels, bias=True, dtype=dtype, device=device)
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(),
            operations.Linear(hidden_size, 2 * hidden_size, bias=True, dtype=dtype, device=device)
        )

    def forward(self, x, c):
        shift, scale = self.adaLN_modulation(c).chunk(2, dim=1)
        x = modulate(self.norm_final(x), shift, scale)
        x = self.linear(x)
        return x

class T2IFinalLayer(nn.Module):
    """
    The final layer of PixArt.
    """

    def __init__(self, hidden_size, patch_size, out_channels, dtype=None, device=None, operations=None):
        super().__init__()
        self.norm_final = operations.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        self.linear = operations.Linear(hidden_size, patch_size * patch_size * out_channels, bias=True, dtype=dtype, device=device)
        self.scale_shift_table = nn.Parameter(torch.randn(2, hidden_size) / hidden_size ** 0.5)
        self.out_channels = out_channels

    def forward(self, x, t):
        dtype = x.dtype
        shift, scale = (self.scale_shift_table[None] + t[:, None]).chunk(2, dim=1)
        x = t2i_modulate(self.norm_final(x), shift, scale)
        x = self.linear(x.to(dtype))
        return x


class MaskFinalLayer(nn.Module):
    """
    The final layer of PixArt.
    """

    def __init__(self, final_hidden_size, c_emb_size, patch_size, out_channels, dtype=None, device=None, operations=None):
        super().__init__()
        self.norm_final = operations.LayerNorm(final_hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        self.linear = operations.Linear(final_hidden_size, patch_size * patch_size * out_channels, bias=True, dtype=dtype, device=device)
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(),
            operations.Linear(c_emb_size, 2 * final_hidden_size, bias=True, dtype=dtype, device=device)
        )
    def forward(self, x, t):
        shift, scale = self.adaLN_modulation(t).chunk(2, dim=1)
        x = modulate(self.norm_final(x), shift, scale)
        x = self.linear(x)
        return x


class DecoderLayer(nn.Module):
    """
    The final layer of PixArt.
    """

    def __init__(self, hidden_size, decoder_hidden_size, dtype=None, device=None, operations=None):
        super().__init__()
        self.norm_decoder = operations.LayerNorm(hidden_size, elementwise_affine=False, eps=1e-6, dtype=dtype, device=device)
        self.linear = operations.Linear(hidden_size, decoder_hidden_size, bias=True, dtype=dtype, device=device)
        self.adaLN_modulation = nn.Sequential(
            nn.SiLU(),
            operations.Linear(hidden_size, 2 * hidden_size, bias=True, dtype=dtype, device=device)
        )
    def forward(self, x, t):
        shift, scale = self.adaLN_modulation(t).chunk(2, dim=1)
        x = modulate(self.norm_decoder(x), shift, scale)
        x = self.linear(x)
        return x


#################################################################################
#               Embedding Layers for Timesteps and Class Labels                 #
#################################################################################
class TimestepEmbedder(nn.Module):
    """
    Embeds scalar timesteps into vector representations.
    """

    def __init__(self, hidden_size, frequency_embedding_size=256, dtype=None, device=None, operations=None):
        super().__init__()
        self.mlp = nn.Sequential(
            operations.Linear(frequency_embedding_size, hidden_size, bias=True, dtype=dtype, device=device),
            nn.SiLU(),
            operations.Linear(hidden_size, hidden_size, bias=True, dtype=dtype, device=device),
        )
        self.frequency_embedding_size = frequency_embedding_size

    @staticmethod
    def timestep_embedding(t, dim, max_period=10000):
        """
        Create sinusoidal timestep embeddings.
        :param t: a 1-D Tensor of N indices, one per batch element.
                          These may be fractional.
        :param dim: the dimension of the output.
        :param max_period: controls the minimum frequency of the embeddings.
        :return: an (N, D) Tensor of positional embeddings.
        """
        # https://github.com/openai/glide-text2im/blob/main/glide_text2im/nn.py
        half = dim // 2
        freqs = torch.exp(
            -math.log(max_period) * torch.arange(start=0, end=half, dtype=torch.float32, device=t.device) / half)
        args = t[:, None].float() * freqs[None]
        embedding = torch.cat([torch.cos(args), torch.sin(args)], dim=-1)
        if dim % 2:
            embedding = torch.cat([embedding, torch.zeros_like(embedding[:, :1])], dim=-1)
        return embedding

    def forward(self, t, dtype):
        t_freq = self.timestep_embedding(t, self.frequency_embedding_size)
        t_emb = self.mlp(t_freq.to(dtype))
        return t_emb


class SizeEmbedder(TimestepEmbedder):
    """
    Embeds scalar timesteps into vector representations.
    """

    def __init__(self, hidden_size, frequency_embedding_size=256, dtype=None, device=None, operations=None):
        super().__init__(hidden_size=hidden_size, frequency_embedding_size=frequency_embedding_size, operations=operations)
        self.mlp = nn.Sequential(
            operations.Linear(frequency_embedding_size, hidden_size, bias=True, dtype=dtype, device=device),
            nn.SiLU(),
            operations.Linear(hidden_size, hidden_size, bias=True, dtype=dtype, device=device),
        )
        self.frequency_embedding_size = frequency_embedding_size
        self.outdim = hidden_size

    def forward(self, s, bs):
        if s.ndim == 1:
            s = s[:, None]
        assert s.ndim == 2
        if s.shape[0] != bs:
            s = s.repeat(bs//s.shape[0], 1)
            assert s.shape[0] == bs
        b, dims = s.shape[0], s.shape[1]
        s = rearrange(s, "b d -> (b d)")
        s_freq = self.timestep_embedding(s, self.frequency_embedding_size)
        s_emb = self.mlp(s_freq.to(s.dtype))
        s_emb = rearrange(s_emb, "(b d) d2 -> b (d d2)", b=b, d=dims, d2=self.outdim)
        return s_emb


class LabelEmbedder(nn.Module):
    """
    Embeds class labels into vector representations. Also handles label dropout for classifier-free guidance.
    """

    def __init__(self, num_classes, hidden_size, dropout_prob, dtype=None, device=None, operations=None):
        super().__init__()
        use_cfg_embedding = dropout_prob > 0
        self.embedding_table = operations.Embedding(num_classes + use_cfg_embedding, hidden_size, dtype=dtype, device=device),
        self.num_classes = num_classes
        self.dropout_prob = dropout_prob

    def token_drop(self, labels, force_drop_ids=None):
        """
        Drops labels to enable classifier-free guidance.
        """
        if force_drop_ids is None:
            drop_ids = torch.rand(labels.shape[0]).cuda() < self.dropout_prob
        else:
            drop_ids = force_drop_ids == 1
        labels = torch.where(drop_ids, self.num_classes, labels)
        return labels

    def forward(self, labels, train, force_drop_ids=None):
        use_dropout = self.dropout_prob > 0
        if (train and use_dropout) or (force_drop_ids is not None):
            labels = self.token_drop(labels, force_drop_ids)
        embeddings = self.embedding_table(labels)
        return embeddings


class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, bias=True, drop=None, dtype=None, device=None, operations=None) -> None:
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features

        self.fc1 = operations.Linear(in_features, hidden_features, bias=bias, dtype=dtype, device=device)
        self.act = act_layer()
        self.fc2 = operations.Linear(hidden_features, out_features, bias=bias, dtype=dtype, device=device)

        self.drop1 = nn.Identity()
        self.drop2 = nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.act(self.fc1(x))
        return self.fc2(x)


class CaptionEmbedder(nn.Module):
    """
    Embeds class labels into vector representations. Also handles label dropout for classifier-free guidance.
    """

    def __init__(self, in_channels, hidden_size, uncond_prob, act_layer=nn.GELU(approximate='tanh'), token_num=120, dtype=None, device=None, operations=None):
        super().__init__()
        self.y_proj = Mlp(
            in_features=in_channels, hidden_features=hidden_size, out_features=hidden_size, act_layer=act_layer,
            dtype=dtype, device=device, operations=operations,
        )
        self.register_buffer("y_embedding", nn.Parameter(torch.randn(token_num, in_channels) / in_channels ** 0.5))
        self.uncond_prob = uncond_prob

    def token_drop(self, caption, force_drop_ids=None):
        """
        Drops labels to enable classifier-free guidance.
        """
        if force_drop_ids is None:
            drop_ids = torch.rand(caption.shape[0]).cuda() < self.uncond_prob
        else:
            drop_ids = force_drop_ids == 1
        caption = torch.where(drop_ids[:, None, None, None], self.y_embedding, caption)
        return caption

    def forward(self, caption, train, force_drop_ids=None):
        if train:
            assert caption.shape[2:] == self.y_embedding.shape
        use_dropout = self.uncond_prob > 0
        if (train and use_dropout) or (force_drop_ids is not None):
            caption = self.token_drop(caption, force_drop_ids)
        caption = self.y_proj(caption)
        return caption


class CaptionEmbedderDoubleBr(nn.Module):
    """
    Embeds class labels into vector representations. Also handles label dropout for classifier-free guidance.
    """

    def __init__(self, in_channels, hidden_size, uncond_prob, act_layer=nn.GELU(approximate='tanh'), token_num=120, dtype=None, device=None, operations=None):
        super().__init__()
        self.proj = Mlp(
            in_features=in_channels, hidden_features=hidden_size, out_features=hidden_size, act_layer=act_layer,
            dtype=dtype, device=device, operations=operations,
        )
        self.embedding = nn.Parameter(torch.randn(1, in_channels) / 10 ** 0.5)
        self.y_embedding = nn.Parameter(torch.randn(token_num, in_channels) / 10 ** 0.5)
        self.uncond_prob = uncond_prob

    def token_drop(self, global_caption, caption, force_drop_ids=None):
        """
        Drops labels to enable classifier-free guidance.
        """
        if force_drop_ids is None:
            drop_ids = torch.rand(global_caption.shape[0]).cuda() < self.uncond_prob
        else:
            drop_ids = force_drop_ids == 1
        global_caption = torch.where(drop_ids[:, None], self.embedding, global_caption)
        caption = torch.where(drop_ids[:, None, None, None], self.y_embedding, caption)
        return global_caption, caption

    def forward(self, caption, train, force_drop_ids=None):
        assert caption.shape[2: ] == self.y_embedding.shape
        global_caption = caption.mean(dim=2).squeeze()
        use_dropout = self.uncond_prob > 0
        if (train and use_dropout) or (force_drop_ids is not None):
            global_caption, caption = self.token_drop(global_caption, caption, force_drop_ids)
        y_embed = self.proj(global_caption)
        return y_embed, caption

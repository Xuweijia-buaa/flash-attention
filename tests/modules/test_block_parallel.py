# Run test with:
# torchrun --no_python --nproc_per_node=8 pytest -q -s tests/modules/test_block_parallel.py

import math
from functools import partial

import torch
import torch.nn as nn
import torch.nn.functional as F
import pytest

from einops import rearrange

from apex.transformer import parallel_state
from apex.transformer import tensor_parallel

from flash_attn.modules.mha import MHA, ParallelMHA
from flash_attn.modules.mlp import FusedDenseGeluDense, ParallelFusedDenseGeluDense
from flash_attn.modules.block import Block

is_sm8x = torch.cuda.get_device_capability('cuda')[0] >= 8


@pytest.mark.parametrize('dtype', [torch.float16] + ([torch.bfloat16] if is_sm8x else []))
# @pytest.mark.parametrize('dtype', [torch.bfloat16])
@pytest.mark.parametrize('world_size', [1, 2, 4, 8])
# @pytest.mark.parametrize('world_size', [2])
@pytest.mark.parametrize('dim', [1024])
def test_block_parallel(dim, world_size, dtype):
    head_dim = 64
    assert dim % head_dim == 0
    num_heads = dim // head_dim
    assert num_heads % world_size == 0
    rtol, atol = (3e-3, 5e-2) if dtype == torch.bfloat16 else (3e-3, 3e-3)
    if not torch.distributed.is_initialized():
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
    device = f'cuda:{torch.distributed.get_rank()}'
    assert world_size <= torch.distributed.get_world_size()
    parallel_state.initialize_model_parallel(tensor_model_parallel_size_=world_size)
    rank = parallel_state.get_tensor_model_parallel_rank()
    # set seed
    torch.random.manual_seed(0)
    batch_size = 8
    seqlen = 1024
    assert (batch_size * seqlen) % world_size == 0
    x_pt = torch.randn(batch_size * seqlen, dim, device=device, dtype=dtype,
                       requires_grad=True)
    residual_pt = torch.randn(batch_size * seqlen, dim, device=device, requires_grad=True)
    # We need to generate g here so that all processes get the same gradient,
    # as rank 0 will have an extra bias that changes the RNG.
    # If we don't divide by batch_size, the gradient gets a bit too large.
    g = torch.randn_like(x_pt) / 32
    x = tensor_parallel.scatter_to_sequence_parallel_region(x_pt).detach().clone().requires_grad_()
    residual = tensor_parallel.scatter_to_sequence_parallel_region(residual_pt).detach().clone().requires_grad_()

    mixer_cls_pt = partial(MHA, num_heads=num_heads, rotary_emb_dim=int(head_dim // 2),
                           use_flash_attn=True, device=device, dtype=dtype)
    mlp_cls_pt = partial(FusedDenseGeluDense, hidden_features=4 * dim,
                         device=device, dtype=dtype)
    norm_cls = partial(nn.LayerNorm, device=device, dtype=dtype)
    model_pt = Block(dim, mixer_cls_pt, mlp_cls_pt, norm_cls, fused_dropout_add_ln=True)
    with torch.no_grad():
        nn.init.normal_(model_pt.norm1.weight)
        nn.init.normal_(model_pt.norm1.bias)
        nn.init.normal_(model_pt.norm2.weight)
        nn.init.normal_(model_pt.norm2.bias)

    mixer_cls = partial(ParallelMHA, num_heads=num_heads,
                        process_group=parallel_state.get_tensor_model_parallel_group(),
                        rotary_emb_dim=int(head_dim // 2), use_flash_attn=True,
                        device=device, dtype=dtype)
    mlp_cls = partial(ParallelFusedDenseGeluDense, hidden_features=4 * dim,
                      process_group=parallel_state.get_tensor_model_parallel_group(),
                      device=device, dtype=dtype)
    model = Block(dim, mixer_cls, mlp_cls, norm_cls, fused_dropout_add_ln=True,
                  sequence_parallel=True)

    partition_dim = dim // world_size
    partition_hidden_dim = 4 * dim // world_size
    with torch.no_grad():
        model.mixer.Wqkv.weight.copy_(
            rearrange(rearrange(model_pt.mixer.Wqkv.weight, '(three o) i -> three o i', three=3)[:, rank * partition_dim:(rank + 1) * partition_dim],
                      'three o i -> (three o) i')
        )
        model.mixer.Wqkv.bias.copy_(
            rearrange(rearrange(model_pt.mixer.Wqkv.bias, '(three o) -> three o', three=3)[:, rank * partition_dim:(rank + 1) * partition_dim],
                      'three o -> (three o)')
        )
        model.mixer.out_proj.weight.copy_(
            model_pt.mixer.out_proj.weight[:, rank * partition_dim:(rank + 1) * partition_dim]
        )
        if rank == 0:
            model.mixer.out_proj.bias.copy_(model_pt.mixer.out_proj.bias)
        model.mlp.fc1.weight.copy_(
            model_pt.mlp.fc1.weight[rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim]
        )
        model.mlp.fc1.bias.copy_(
            model_pt.mlp.fc1.bias[rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim]
        )
        model.mlp.fc2.weight.copy_(
            model_pt.mlp.fc2.weight[:, rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim]
        )
        if rank == 0:
            model.mlp.fc2.bias.copy_(model_pt.mlp.fc2.bias)
        model.norm1.weight.copy_(model_pt.norm1.weight)
        model.norm1.bias.copy_(model_pt.norm1.bias)
        model.norm2.weight.copy_(model_pt.norm2.weight)
        model.norm2.bias.copy_(model_pt.norm2.bias)

    mixer_kwargs = {'seqlen': seqlen}
    out, out_residual = model(x, residual, mixer_kwargs=mixer_kwargs)
    out_pt, out_residual_pt = model_pt(rearrange(x_pt, '(b s) d -> b s d', s=seqlen),
                                       rearrange(residual_pt, '(b s) d -> b s d', s=seqlen))
    out_pt, out_residual_pt = [rearrange(x, 'b s d -> (b s) d') for x in [out_pt, out_residual_pt]]
    partition_batch_dim = batch_size * seqlen // world_size
    assert torch.allclose(
        out, out_pt[rank * partition_batch_dim:(rank + 1) * partition_batch_dim],
        rtol=rtol, atol=atol
    )
    assert torch.allclose(
        out_residual, out_residual_pt[rank * partition_batch_dim:(rank + 1) * partition_batch_dim],
        rtol=rtol, atol=atol
    )

    out_pt.backward(g)
    out.backward(g[rank * partition_batch_dim:(rank + 1) * partition_batch_dim])
    # We want to iterate over parameters with _sequence_parallel=True in the same order,
    # as different ranks might have different number of parameters (e.g., only rank 0 has bias).
    params_seqparallel = {name: p for name, p in model.named_parameters()
                          if getattr(p, '_sequence_parallel', False)}
    for _, p in sorted(params_seqparallel.items()):
        if getattr(p, '_sequence_parallel', False):
            torch.distributed.all_reduce(p.grad, group=parallel_state.get_tensor_model_parallel_group())
    parallel_state.destroy_model_parallel()

    assert torch.allclose(
        x.grad, x_pt.grad[rank * partition_batch_dim:(rank + 1) * partition_batch_dim],
        rtol=rtol, atol=atol
    )
    assert torch.allclose(
        residual.grad, residual_pt.grad[rank * partition_batch_dim:(rank + 1) * partition_batch_dim],
        rtol=rtol, atol=atol
    )
    # The error for d_weight and d_bias is quite a bit higher
    assert torch.allclose(
        model.mixer.Wqkv.weight.grad,
        rearrange(rearrange(model_pt.mixer.Wqkv.weight.grad, '(three o) i -> three o i', three=3)[:, rank * partition_dim:(rank + 1) * partition_dim],
                  'three o i -> (three o) i'),
        rtol=rtol, atol=atol * 10
    )
    assert torch.allclose(
        model.mixer.Wqkv.bias.grad,
        rearrange(rearrange(model_pt.mixer.Wqkv.bias.grad, '(three o) -> three o', three=3)[:, rank * partition_dim:(rank + 1) * partition_dim],
                  'three o -> (three o)'),
        rtol=rtol, atol=atol * 5
    )
    assert torch.allclose(
        model.mixer.out_proj.weight.grad,
        model_pt.mixer.out_proj.weight.grad[:, rank * partition_dim:(rank + 1) * partition_dim],
        rtol=rtol, atol=atol * 10
    )
    if rank == 0:
        assert torch.allclose(model.mixer.out_proj.bias.grad, model_pt.mixer.out_proj.bias.grad, rtol=rtol, atol=atol * 5)
    assert torch.allclose(
        model.mlp.fc1.weight.grad,
        model_pt.mlp.fc1.weight.grad[rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim],
        rtol=rtol, atol=atol * 10
    )
    assert torch.allclose(
        model.mlp.fc1.bias.grad,
        model_pt.mlp.fc1.bias.grad[rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim],
        rtol=rtol, atol=atol * 5
    )
    assert torch.allclose(
        model.mlp.fc2.weight.grad,
        model_pt.mlp.fc2.weight.grad[:, rank * partition_hidden_dim:(rank + 1) * partition_hidden_dim],
        rtol=rtol, atol=atol * 10
    )
    if rank == 0:
        assert torch.allclose(model.mlp.fc2.bias.grad, model_pt.mlp.fc2.bias.grad,
                              rtol=rtol, atol=atol * 5)

    assert torch.allclose(model.norm1.weight.grad, model_pt.norm1.weight.grad, rtol=rtol, atol=atol * 5)
    assert torch.allclose(model.norm1.bias.grad, model_pt.norm1.bias.grad, rtol=rtol, atol=atol * 5)
    assert torch.allclose(model.norm2.weight.grad, model_pt.norm2.weight.grad, rtol=rtol, atol=atol * 5)
    assert torch.allclose(model.norm2.bias.grad, model_pt.norm2.bias.grad, rtol=rtol, atol=atol * 5)

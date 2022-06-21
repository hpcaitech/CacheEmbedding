import torch

from recsys import DISTMGR as dist_manager


def _reduce(x, parallel_mode):
    if dist_manager.get_world_size(parallel_mode) == 1:
        return x

    process_group = dist_manager.get_cpu_group(parallel_mode) \
        if x.device.type == 'cpu' else dist_manager.get_group(parallel_mode)
    torch.distributed.all_reduce(x, group=process_group)
    return x


def _gather(x, parallel_mode, dim):
    world_size = dist_manager.get_world_size(parallel_mode)
    if world_size == 1:
        return x

    rank = dist_manager.get_rank(parallel_mode)
    process_group = dist_manager.get_cpu_group(parallel_mode) \
        if x.device.type == 'cpu' else dist_manager.get_group(parallel_mode)

    tensor_list = [torch.empty_like(x) if i != rank else x for i in range(world_size)]
    torch.distributed.all_gather(tensor_list, x, group=process_group)
    return torch.cat(tensor_list, dim=dim).contiguous()


def _tensor_gather(x, parallel_mode, dim):
    world_size = dist_manager.get_world_size(parallel_mode)
    if world_size == 1:
        return x

    rank = dist_manager.get_rank(parallel_mode)
    process_group = dist_manager.get_cpu_group(parallel_mode) \
        if x.device.type == 'cpu' else dist_manager.get_group(parallel_mode)

    tensor_list = [None if i != rank else x for i in range(world_size)]
    torch.distributed.all_gather_object(tensor_list, x, group=process_group)
    result = torch.cat([each.to(x.device) for each in tensor_list], dim=dim).contiguous()

    return result


def _tensor_split(x, parallel_mode, dim):
    world_size = dist_manager.get_world_size(parallel_mode)
    if world_size == 1:
        return x

    rank = dist_manager.get_rank(parallel_mode)
    tensor = torch.tensor_split(x, world_size, dim=dim)[rank]
    return tensor


def _all_to_all(x, parallel_mode, scatter_dim, gather_dim):
    world_size = dist_manager.get_world_size(parallel_mode)
    if world_size == 1:
        return x

    # TODO: enabling mpi backend to support CPU all_to_all
    assert x.device.type == 'cuda', f"Currently, the collective function dual_all_to_all only supports nccl backend"
    process_group = dist_manager.get_group(parallel_mode)

    shapes = list(x.size())
    shapes[scatter_dim] = shapes[scatter_dim] // world_size

    scatter_list = [each.contiguous() for each in torch.tensor_split(x, world_size, scatter_dim)]
    gather_list = [torch.empty(*shapes, dtype=x.dtype, device=x.device) for _ in range(world_size)]
    torch.distributed.all_to_all(gather_list, scatter_list, group=process_group)

    return torch.cat(gather_list, dim=gather_dim).contiguous()


class _ReduceForward(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, parallel_mode):
        return _reduce(x, parallel_mode)

    @staticmethod
    def backward(ctx, grad):
        return grad, None


def reduce_forward(x, parallel_mode):
    return _ReduceForward.apply(x, parallel_mode)


class _TensorGatherForwardSplitBackward(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, parallel_mode, dim):
        ctx.parallel_mode = parallel_mode
        ctx.dim = dim
        return _tensor_gather(x, parallel_mode, dim)

    @staticmethod
    def backward(ctx, grad):
        return _tensor_split(grad, ctx.parallel_mode, ctx.dim), None, None


def tensor_gather_forward_split_backward(x, parallel_mode, dim):
    return _TensorGatherForwardSplitBackward.apply(x, parallel_mode, dim)


class _GatherForwardSplitBackward(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, parallel_mode, dim):
        ctx.parallel_mode = parallel_mode
        ctx.dim = dim
        return _gather(x, parallel_mode, dim)

    @staticmethod
    def backward(ctx, grad):
        return _tensor_split(grad, ctx.parallel_mode, ctx.dim), None, None


def gather_forward_split_backward(x, parallel_mode, dim):
    return _GatherForwardSplitBackward.apply(x, parallel_mode, dim)


class _SplitForwardGatherBackward(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, parallel_mode, dim):
        ctx.parallel_mode = parallel_mode
        ctx.dim = dim
        return _tensor_split(x, parallel_mode, dim)

    @staticmethod
    def backward(ctx, grad):
        return _gather(grad, ctx.parallel_mode, ctx.dim), None, None


def split_forward_gather_backward(x, parallel_mode, dim):
    return _SplitForwardGatherBackward.apply(x, parallel_mode, dim)


class _DualAllToAll(torch.autograd.Function):

    @staticmethod
    def forward(ctx, x, parallel_mode, scatter_dim, gather_dim):
        ctx.parallel_mode = parallel_mode
        ctx.scatter_dim = scatter_dim
        ctx.gather_dim = gather_dim

        return _all_to_all(x, parallel_mode, scatter_dim, gather_dim)

    @staticmethod
    def backward(ctx, grad):
        return _all_to_all(grad, ctx.parallel_mode, ctx.gather_dim, ctx.scatter_dim), None, None, None


def dual_all_to_all(x, parallel_mode, scatter_dim, gather_dim):
    return _DualAllToAll.apply(x, parallel_mode, scatter_dim, gather_dim)

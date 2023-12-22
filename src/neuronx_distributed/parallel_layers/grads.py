import os

import torch
import torch_xla.core.xla_model as xm

from ..utils.logger import get_logger
from .layers import param_is_not_tensor_parallel_duplicate
from .mappings import reduce_from_tensor_model_parallel_region
from .parallel_state import (
    get_data_parallel_group,
    get_data_parallel_size,
    get_pipeline_model_parallel_group,
    get_pipeline_model_parallel_size,
    get_tensor_model_parallel_group,
    get_tensor_model_parallel_size,
    rmsg,
)

logger = get_logger()

# allreduce bucket buffer size
_ALLREDUCE_BUCKET_CAP_MB = 512  # MB


def param_is_not_shared(param):
    return not hasattr(param, "shared") or not param.shared


def get_grad_norm(parameters, norm_type=2, zero1_optimizer=False, zero1_optimizer_groups=None, force_spmd=True):
    """Get gradient norm of an iterable of parameters.

    This can handle model parallel parameters.

    Arguments:
        parameters (Iterable[Tensor] or Tensor): an iterable of Tensors or a
            single Tensor that will have gradients normalized
        norm_type (float or int): type of the used p-norm. Can be ``'inf'`` for
            infinity norm.
        zero1_optimizer(bool): Whether zero1 optimizer is used, if so we will collect
            grad norm from the world group
        force_spmd(bool): Whether to force spmd when calculating global norm. If set to
            True we will sum the tp duplicated paramter grads on all ranks and divide them
            by tp size. Warning: If the grads are too small the division might result in
            incorrect results.

    Returns:
        Total norm of the parameters (viewed as a single vector).
    """
    if torch.isinf(torch.tensor(norm_type)):
        # Always use spmd for inf norm since it is using MAX operation
        force_spmd = True

    if not zero1_optimizer and zero1_optimizer_groups is not None:
        raise ValueError(
            f"Getting zero1_optimizer_groups while zero1_optimizer is False. When using zero-1 optimizer grad clipping is handled by optimizer."
        )  # noqa

    def _allreduce_norm_across_parallel_groups(total_norm, reduce_op):
        """
        - zero1 without groups: allreduce across world groups
        - otherwise allreduce across each parallel group
        """
        if zero1_optimizer and zero1_optimizer_groups is None:
            torch.distributed.all_reduce(total_norm, op=reduce_op)
        else:
            if get_tensor_model_parallel_size() > 1:
                torch.distributed.all_reduce(
                    total_norm,
                    op=reduce_op,
                    group=get_tensor_model_parallel_group(),
                )
            if get_pipeline_model_parallel_size() > 1:
                torch.distributed.all_reduce(
                    total_norm,
                    op=reduce_op,
                    group=get_pipeline_model_parallel_group(),
                )
            if zero1_optimizer_groups is not None:
                xm.all_reduce(
                    xm.REDUCE_SUM,
                    [total_norm],
                    groups=zero1_optimizer_groups,
                    pin_layout=True,
                )

    device = xm.xla_device()

    if isinstance(parameters, torch.Tensor):
        parameters = [parameters]

    grads = []
    grads_for_norm = []
    grads_for_norm_tp_duplicated = []
    for param in parameters:
        grad_not_none = param.grad is not None
        is_not_shared = param_is_not_shared(param)
        is_not_tp_duplicate = param_is_not_tensor_parallel_duplicate(param)
        is_tp_param = hasattr(param, "tensor_model_parallel") and param.tensor_model_parallel
        if grad_not_none:
            grad = param.grad.detach()
            grads.append(grad)
        if grad_not_none and is_not_shared:
            if is_tp_param or (is_not_tp_duplicate and not force_spmd):
                # TP parallelized parameters
                # (not force_spmd) only tp rank 0 will add non-tp paramaters
                grads_for_norm.append(grad)
            elif force_spmd:
                # non-tp paramaters
                grads_for_norm_tp_duplicated.append(grad)

    # Norm parameters.
    norm_type = float(norm_type)
    total_norm = torch.FloatTensor([float(0.0)]).to(device)

    # Calculate norm.
    if torch.isinf(torch.tensor(norm_type)):
        total_norm_tp_duplicated = max(grad.abs().max() for grad in grads_for_norm_tp_duplicated)
        total_norm = max(grad.abs().max() for grad in grads_for_norm)
        total_norm = max(total_norm_tp_duplicated, total_norm)
        total_norm = torch.FloatTensor([float(total_norm)]).to(device)
        _allreduce_norm_across_parallel_groups(total_norm, torch.distributed.ReduceOp.MAX)
        total_norm = total_norm[0].item()
    else:
        if force_spmd:
            # sum the non-tp grad norm and scale by the tp_size
            for grad in grads_for_norm_tp_duplicated:
                grad_norm = torch.norm(grad, norm_type)
                total_norm += grad_norm**norm_type
            total_norm /= get_tensor_model_parallel_size()

        for grad in grads_for_norm:
            grad_norm = torch.norm(grad, norm_type)
            total_norm += grad_norm**norm_type

        _allreduce_norm_across_parallel_groups(total_norm, torch.distributed.ReduceOp.SUM)
        total_norm = torch.pow(total_norm, 1.0 / norm_type)

    return total_norm


def clip_grad_norm(
    parameters, max_norm, norm_type=2, zero1_optimizer=False, zero1_optimizer_groups=None, force_spmd=True
):
    """Clips gradient norm of an iterable of parameters.

    This is adapted from torch.nn.utils.clip_grad.clip_grad_norm_ and
    added functionality to handle model parallel parameters. Note that
    the gradients are modified in place.

    Arguments:
        parameters (Iterable[Tensor] or Tensor): an iterable of Tensors or a
            single Tensor that will have gradients normalized
        max_norm (float or int): max norm of the gradients
        norm_type (float or int): type of the used p-norm. Can be ``'inf'`` for
            infinity norm.
        zero1_optimizer(bool): Whether zero1 optimizer is used, if so we will collect
            grad norm from the world group
        force_spmd(bool): Whether to force spmd when calculating global norm. If set to
            True we will sum the tp duplicated paramter grads on all ranks and divide them
            by tp size. Warning: If the grads are too small the division might result in
            incorrect results.

    Returns:
        Total norm of the parameters (viewed as a single vector).
    """

    if not isinstance(parameters, list):
        parameters = list(parameters)

    # Get norm
    total_norm = get_grad_norm(
        parameters,
        norm_type=norm_type,
        zero1_optimizer=zero1_optimizer,
        zero1_optimizer_groups=zero1_optimizer_groups,
        force_spmd=force_spmd,
    )

    # Scale.
    device = xm.xla_device()
    grads = []
    for param in parameters:
        if param.grad is not None:
            grad = param.grad.detach()
            grads.append(grad)

    clip_coeff = max_norm / (total_norm + 1.0e-6)
    for g in grads:
        g.data.mul_(torch.where(clip_coeff < 1, clip_coeff, torch.tensor(1.0, device=device)))
    return total_norm


def bucket_allreduce_gradients(grads_list):
    """
    All reduce bucket gradients for data parallelism.
    Referred from https://code.amazon.com/packages/Neuron-Nemo-Megatron/blobs/899fc918ffa82e4bea46750ff6dfe5b909d144a9/--/nemo/nemo/collections/nlp/models/language_modeling/megatron_base_model.py#L57 # noqa: E501
    """
    bucket_cap = int(os.getenv("ALLREDUCE_BUCKET_CAP_MB", _ALLREDUCE_BUCKET_CAP_MB)) * 1024 * 1024

    dtype_groups = {}
    for grad in grads_list:
        tp = grad.dtype
        if tp not in dtype_groups:
            dtype_groups[tp] = []
        dtype_groups[tp].append(grad)
    logger.debug(rmsg(f"reduce grads dtype_groups counts {[(tp, len(group)) for tp, group in dtype_groups.items()]}"))

    for tp in dtype_groups:
        grads = dtype_groups[tp]

        # Reverse the gradients list so that we start allreduce from the last layer
        # onwards. This allows allreduce to trigger as soon as the bucket fills up and
        # overlap with backward pass.
        gradients = reversed(grads)
        total = 0
        tensor_bucket = []
        groups = get_data_parallel_group(as_list=True)

        for grad in gradients:
            grad.data /= get_data_parallel_size()
            grad_bytes = grad.numel() * grad.element_size()

            # Gradient is larger than bucket_cap, don't bucketize
            if grad_bytes > bucket_cap:
                # Flush out previous buckets even if they don't fill up
                # This maintains the strict reverse ordering
                if len(tensor_bucket):
                    xm.all_reduce("sum", tensor_bucket, groups=groups)
                    total = 0
                    tensor_bucket = []
                xm.all_reduce("sum", [grad], groups=groups)
                continue

            # Bucketize till the total spills over
            total += grad_bytes
            if total > bucket_cap:
                logger.debug(rmsg(f"all_reduce for total {total} bytes with groups {groups}"))
                xm.all_reduce("sum", tensor_bucket, groups=groups)
                total = grad_bytes
                tensor_bucket = []
            tensor_bucket.append(grad)

        # Flush the last remaining bucket
        if len(tensor_bucket):
            logger.debug(rmsg(f"all_reduce last bucket of {len(tensor_bucket)} tensors with groups {groups}"))
            xm.all_reduce("sum", tensor_bucket, groups=groups)


def allreduce_sequence_parallel_gradients(optimizer):
    """All-reduce layernorm parameters across model parallel nodes when sequence parallelism is used.
    Modified from megatron-lm:
    https://gitlab-master.nvidia.com/ADLR/megatron-lm/-/blob/3f91f09bb2ab32f9904b47f46f19d2fc3f518ed8/megatron/training.py#L425
    """
    grads = []
    for param_group in optimizer.__getstate__()["param_groups"]:
        for group, params in param_group.items():
            if group == "params":
                for p in params:
                    if isinstance(p, torch.Tensor) and p.grad is not None:
                        sequence_parallel_param = getattr(p, "sequence_parallel_enabled", False)
                        if sequence_parallel_param:
                            grads.append(p.grad.data)
    for grad in grads:
        reduce_from_tensor_model_parallel_region(grad)

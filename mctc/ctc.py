# AUTOGENERATED! DO NOT EDIT! File to edit: notebooks/01_CTC_loss.ipynb (unless otherwise specified).

__all__ = ['device', 'generate_sample_inputs', 'ctc_loss_pytorch', 'interleave_blanks', 'prepare_inputs',
           'ctc_loss_basic', 'semiring', 'neginf', 'Log', 'ctc_fwd_bwd', 'masked_grad', 'ctc_loss_py', 'ctc_loss_cupy',
           'cupy_funcs', 'max_grad', 'Max', 'viterbi_alignments', 'soft_alignments', 'ctc_loss_direct_cupy', 'Prob']

# Cell
import numpy as np
import cupy as cp
import torch
import torch.nn as nn
from collections import namedtuple
from .utils import *

device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

# Cell
def generate_sample_inputs(T_min, T_max, N, C, L_min, L_max, device=device):
    """
    Args:
        T_min, T_max: bounds on number of time steps
        N: batch size
        C: alphabet size (including blank)
        L_min, L_max: bounds on target length
    """
    logits = torch.randn(T_max, N, C, device=device, requires_grad=True)
    targets = torch.randint(1, C, (N, L_max), dtype=torch.long, device=device)
    input_lengths = torch.randint(T_min, T_max+1, (N,), dtype=torch.long, device=device)
    target_lengths = torch.randint(L_min, L_max+1, (N,), dtype=torch.long, device=device)
    return logits, targets, input_lengths, target_lengths

# Cell
def ctc_loss_pytorch(logits, targets, input_lengths, target_lengths, blank=0, reduction='mean', zero_infinity=False):
    log_probs = logits.log_softmax(2)
    return nn.functional.ctc_loss(log_probs, targets, input_lengths, target_lengths, blank, reduction, zero_infinity)

# Cell
semiring = namedtuple('semiring', ('zero', 'one', 'mul', 'sum'))
neginf = -1e38
Log = semiring(zero=neginf, one=0., mul=torch.add, sum=torch.logsumexp)

def interleave_blanks(targets, blank_idx: int):
    N, L = targets.shape
    interleaved = targets.new_full((N, 2*L+1), blank_idx)
    interleaved[:, 1::2] = targets
    return interleaved

def prepare_inputs(scores, targets, input_lengths, target_lengths):
    states = interleave_blanks(targets, blank_idx=0)
    state_scores = torch.gather(scores, 2, states.expand(scores.size(0), -1, -1))
    repeat_mask = torch.nn.functional.pad(states[:, 2:] == states[:, :-2], (2, 0), value=0.)
    final_states = torch.stack([target_lengths*2-1, target_lengths*2], 1)
    return state_scores, repeat_mask, final_states, input_lengths

def _ctc_logz_fwd(state_scores, repeat_mask, final_states, input_lengths, S:semiring=Log):
    T, N, Lp = state_scores.shape
    zeros = state_scores.new_full((N, Lp), S.zero)
    a = state_scores.new_full((N, Lp+2), S.zero)
    a[:, 2] = S.one
    alpha = torch.empty_like(state_scores)
    for t in range(0, T):
        alpha[t] = a[:, 2:] = S.mul(state_scores[t], S.sum(
            torch.stack([a[:, 2:], a[:, 1:-1], torch.where(repeat_mask, zeros, a[:, :-2])]), dim=0))
    return S.sum(alpha[input_lengths-1, torch.arange(N)].gather(1, final_states), dim=1)

def ctc_loss_basic(logits, targets, input_lengths, target_lengths):
    log_probs = logits.log_softmax(2)
    logz = _ctc_logz_fwd(*prepare_inputs(log_probs, targets, input_lengths, target_lengths))
    return -(logz / target_lengths).mean()

# Cell
def ctc_fwd_bwd(state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl, S:semiring=Log):
    T, N, Lp = state_scores.shape
    alpha, beta = [state_scores.new_full((T+1, N, Lp), S.zero) for _ in range(2)]
    alpha[0, :, 0] = S.one
    beta[input_lengths, torch.arange(N)] = state_scores.new_full((N, Lp), S.zero).scatter_(1, final_states, S.one)
    alpha_T = fwd_bwd_impl(alpha, beta, state_scores, repeat_mask, input_lengths, S)
    logz = S.sum(alpha_T.gather(1, final_states), dim=1)
    return alpha, beta, logz

def _ctc_fwd_bwd_py(alpha, beta, state_scores, repeat_mask, input_lengths, S:semiring=Log):
    T, N, Lp = state_scores.shape
    zeros = alpha.new_full((1,), S.zero)
    #fwd
    a = torch.cat([alpha.new_full((N, 2), S.zero), alpha[0]], 1)
    for t in range(0, T):
        a[:, 2:] = S.mul(state_scores[t], S.sum(torch.stack([a[:, 2:], a[:, 1:-1], torch.where(repeat_mask, zeros, a[:, :-2])]), dim=0))
        alpha[t+1] = a[:, 2:]
    #bwd
    b = alpha.new_full((N, Lp+2), S.zero)
    repeat_mask = torch.cat([repeat_mask[:, 2:], repeat_mask[:, :2]], 1)
    for t in range(T, 0, -1):
        b[:, :-2] = S.mul(beta[t], state_scores[t-1])
        b[:, :-2] = S.sum(torch.stack([b[:, :-2], b[:, 1:-1], torch.where(repeat_mask, zeros, b[:, 2:])]), dim=0)
        beta[t-1, t <= input_lengths] = b[t <= input_lengths, :-2]

    return alpha[input_lengths, torch.arange(N)]

def masked_grad(grad, input_lengths):
    input_mask = (torch.arange(grad.size(0), device=grad.device)[:, None] < input_lengths)
    return torch.where(input_mask, grad, grad.new_zeros((1,))).unsqueeze(2)

class _CTCLogz(torch.autograd.Function):
    @staticmethod
    def forward(ctx, state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl):
        alpha, beta, logz = ctc_fwd_bwd(state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl, Log)
        ctx.save_for_backward(alpha, beta, input_lengths)
        return logz

    @staticmethod
    def backward(ctx, grad):
        alpha, beta, input_lengths = ctx.saved_tensors
        g = torch.softmax(alpha[1:] + beta[1:], dim=2) * masked_grad(grad.expand(alpha.size(0)-1, -1), input_lengths)
        return g, None, None, None, None

def ctc_loss_py(logits, targets, input_lengths, target_lengths):
    logz = _CTCLogz.apply(*prepare_inputs(logits.log_softmax(2), targets, input_lengths, target_lengths), _ctc_fwd_bwd_py)
    return - (logz / target_lengths).mean()

# Cell
cupy_funcs = {
    (torch.float32, Log): load_cupy_func('cuda/ctc.cu', 'ctc_fwd_bwd_logspace', FLOAT='float',  SUM='logsumexp3', MUL='add', ZERO=f'{neginf:E}'),
    (torch.float64, Log): load_cupy_func('cuda/ctc.cu', 'ctc_fwd_bwd_logspace', FLOAT='double', SUM='logsumexp3', MUL='add', ZERO=f'{neginf:E}'),
}

def _ctc_fwd_bwd_cupy(alpha, beta, state_scores, repeat_mask, input_lengths, S:semiring):
    T, N, Lp = state_scores.shape
    alpha_T = torch.empty_like(alpha[0])
    with cp.cuda.Device(state_scores.device.index):
        cupy_funcs[(state_scores.dtype, S)](grid=(N, 2, 1), block=(Lp, 1, 1), shared_mem=2*8*Lp,
               args=(alpha_T.data_ptr(), alpha.data_ptr(), beta.data_ptr(), state_scores.data_ptr(), repeat_mask.data_ptr(),
                     input_lengths.data_ptr(), N, Lp))
    return alpha_T

def ctc_loss_cupy(logits, targets, input_lengths, target_lengths):
    logz = _CTCLogz.apply(*prepare_inputs(logits.log_softmax(2), targets, input_lengths, target_lengths), _ctc_fwd_bwd_cupy)
    return - (logz / target_lengths).mean()

# Cell
def max_grad(x, dim=0):
    return torch.zeros_like(x).scatter_(dim, x.argmax(dim, True), 1.0)

Max = semiring(zero=neginf, one=0., mul=torch.add, sum=(lambda x, dim=0: torch.max(x, dim=dim)[0]))
cupy_funcs[(torch.float32, Max)] = load_cupy_func('cuda/ctc.cu', 'ctc_fwd_bwd_logspace', FLOAT='float',  SUM='max3', MUL='add', ZERO=f'{neginf:E}')
cupy_funcs[(torch.float64, Max)] = load_cupy_func('cuda/ctc.cu', 'ctc_fwd_bwd_logspace', FLOAT='double', SUM='max3', MUL='add', ZERO=f'{neginf:E}')

class _CTCLogzViterbi(torch.autograd.Function):
    @staticmethod
    def forward(ctx, state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl):
        alpha, beta, logz = ctc_fwd_bwd(state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl, Max)
        ctx.save_for_backward(alpha, beta, input_lengths)
        return logz

    @staticmethod
    def backward(ctx, grad):
        alpha, beta, input_lengths = ctx.saved_tensors
        g = max_grad(alpha[1:] + beta[1:], dim=2) * masked_grad(grad.expand(alpha.size(0)-1, -1), input_lengths)
        return g, None, None, None, None

# Cell
def viterbi_alignments(logits, targets, input_lengths, target_lengths):
    state_scores, repeat_mask, final_states, input_lengths = prepare_inputs(logits.log_softmax(2), targets, input_lengths, target_lengths)
    _CTCLogzViterbi.apply(state_scores.detach_().requires_grad_(), repeat_mask, final_states, input_lengths, _ctc_fwd_bwd_cupy).sum().backward()
    return state_scores.grad

def soft_alignments(logits, targets, input_lengths, target_lengths, beta=1.0):
    state_scores, repeat_mask, final_states, input_lengths = prepare_inputs((logits*beta).log_softmax(2), targets, input_lengths, target_lengths)
    _CTCLogz.apply(state_scores.detach_().requires_grad_(), repeat_mask, final_states, input_lengths, _ctc_fwd_bwd_cupy).sum().backward()
    return state_scores.grad

# Cell
Prob = semiring(zero=0., one=1., mul=torch.mul, sum=torch.sum)
cupy_funcs[(torch.float64, Prob)] = load_cupy_func('cuda/ctc.cu', 'ctc_fwd_bwd_logspace', FLOAT='double', SUM='sum3', MUL='mul', ZERO='0.0')

class _CTCLogzDirect(torch.autograd.Function):
    @staticmethod
    def forward(ctx, state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl):
        alpha, beta, z = ctc_fwd_bwd(state_scores, repeat_mask, final_states, input_lengths, fwd_bwd_impl, Prob)
        ctx.save_for_backward(alpha, beta, state_scores, input_lengths)
        return torch.log(z)

    @staticmethod
    def backward(ctx, grad):
        alpha, beta, state_probs, input_lengths = ctx.saved_tensors
        g = alpha[1:]*beta[1:]
        g = (g/state_probs)*masked_grad(grad.expand(alpha.size(0)-1, -1), input_lengths)/(g.sum(-1, keepdim=True)+1e-38)
        return g, None, None, None, None

def ctc_loss_direct_cupy(logits, targets, input_lengths, target_lengths):
    logz = _CTCLogzDirect.apply(*prepare_inputs(logits.softmax(2), targets, input_lengths, target_lengths), _ctc_fwd_bwd_cupy)
    return - (logz / target_lengths).mean()
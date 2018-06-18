from __future__ import absolute_import, division, print_function

import math

import torch
from torch.autograd import Function
from torch.autograd.function import once_differentiable
from torch.distributions import Categorical, constraints

from pyro.distributions.torch_distribution import TorchDistribution
from pyro.distributions.util import sum_leftmost


class MixtureOfDiagNormalsSharedCovariance(TorchDistribution):
    """Mixture of normal distributions with diagonal covariance matrices.
    This distribution supports pathwise derivatives.

    :param torch.Tensor locs: K x D mean matrix
    :param torch.Tensor scale: shared D-dimensional scale vector
    :param torch.Tensor logits: K-dimensional vector of softmax logits
    """
    has_rsample = True
    arg_constraints = {"locs": constraints.real, "scale": constraints.positive,
                       "logits": constraints.real}

    def __init__(self, locs, scale, logits):
        self.batch_mode = (locs.dim() > 2)
        assert(self.batch_mode or locs.dim() == 2), \
            "The locs parameter in MixtureOfDiagNormals should be K x D dimensional (or ... x B x K x D in batch mode)"
        if not self.batch_mode:
            assert(scale.dim() == 1), "The scale parameter in MixtureOfDiagNormals should be D dimensional"
            assert(logits.dim() == 1), "The logits parameter in MixtureOfDiagNormals should be K dimensional"
            assert(logits.size(0) == locs.size(0))
            batch_shape = ()
        else:
            assert(scale.dim() > 1), "The scale parameter in MixtureOfDiagNormals should be ... x B x D dimensional"
            assert(logits.dim() > 1), "The logits parameter in MixtureOfDiagNormals should be ... x B x K dimensional"
            assert(logits.size(-1) == locs.size(-2))
            batch_shape = tuple(locs.shape[:-2])
        self.locs = locs
        self.scale = scale
        self.logits = logits
        self.dim = locs.size(-1)
        self.categorical = Categorical(logits=logits)
        self.probs = self.categorical.probs
        super(MixtureOfDiagNormalsSharedCovariance, self).__init__(batch_shape=batch_shape, event_shape=(self.dim,))

    def log_prob(self, value):
        scale = self.scale.unsqueeze(-2) if self.batch_mode else self.scale
        epsilon = (value.unsqueeze(-2) - self.locs) / scale  # L B K D
        eps_sqr = 0.5 * torch.pow(epsilon, 2.0).sum(-1)  # L B K
        eps_sqr_min = torch.min(eps_sqr, -1)[0]  # L B K
        result = self.probs * torch.exp(-eps_sqr + eps_sqr_min.unsqueeze(-1))  # L B K
        result = torch.log(result.sum(-1))  # L B
        result -= 0.5 * math.log(2.0 * math.pi) * float(self.dim)
        result -= torch.log(self.scale).sum(-1)
        result -= eps_sqr_min
        return result

    def rsample(self, sample_shape=torch.Size()):
        which = self.categorical.sample(sample_shape)
        return _MixDiagNormalSharedCovarianceSample.apply(self.locs, self.scale, self.logits, self.probs,
                                                          which, sample_shape + self.scale.shape)


class _MixDiagNormalSharedCovarianceSample(Function):
    @staticmethod
    def forward(ctx, locs, scale, logits, pis, which, noise_shape):
        dim = scale.size(-1)
        white = locs.new(noise_shape).normal_()
        n_unsqueezes = locs.dim() - which.dim()
        for _ in range(n_unsqueezes):
            which = which.unsqueeze(-1)
        expand_tuple = tuple(which.shape[:-1] + (dim,))
        loc = torch.gather(locs, -2, which.expand(expand_tuple)).squeeze(-2)
        z = loc + scale * white
        ctx.save_for_backward(z, scale, locs, logits, pis)
        return z

    @staticmethod
    @once_differentiable
    def backward(ctx, grad_output):

        z, scale, locs, logits, pis = ctx.saved_tensors
        K = logits.size(-1)
        batch_dims = scale.dim() - 1
        g = grad_output  # l b i

        z_tilde = z / scale  # l b i
        locs_tilde = locs / scale.unsqueeze(-2)  # b j i
        mu_ab = locs_tilde.unsqueeze(-2) - locs_tilde.unsqueeze(-3)  # b k j i
        mu_ab_norm = torch.pow(mu_ab, 2.0).sum(-1).sqrt()  # b k j
        mu_ab /= mu_ab_norm.unsqueeze(-1)  # b k j i
        diagonals = torch.LongTensor(range(K))
        mu_ab[..., diagonals, diagonals, :] = 0.0

        mu_ll_ab = (locs_tilde.unsqueeze(-2) * mu_ab).sum(-1)  # b k j
        z_ll_ab = (z_tilde.unsqueeze(-2).unsqueeze(-2) * mu_ab).sum(-1)  # l b k j
        z_perp_ab = z_tilde.unsqueeze(-2).unsqueeze(-2) - z_ll_ab.unsqueeze(-1) * mu_ab  # l b k j i
        z_perp_ab_sqr = torch.pow(z_perp_ab, 2.0).sum(-1)  # l b k j

        epsilons = z_tilde.unsqueeze(-2) - locs_tilde  # l b j i
        log_qs = -0.5 * torch.pow(epsilons, 2.0)   # l b j i
        log_q_j = log_qs.sum(-1, keepdim=True)     # l b j 1
        log_q_j_max = torch.max(log_q_j, -2, keepdim=True)[0]
        q_j_prime = torch.exp(log_q_j - log_q_j_max)  # l b j 1
        q_j = torch.exp(log_q_j)  # l b j 1

        q_tot = (pis.unsqueeze(-1) * q_j).sum(-2)  # l b 1
        q_tot_prime = (pis.unsqueeze(-1) * q_j_prime).sum(-2).unsqueeze(-1)  # l b 1 1

        root_two = math.sqrt(2.0)
        mu_ll_ba = torch.transpose(mu_ll_ab, -1, -2)
        logits_grad = torch.erf((z_ll_ab - mu_ll_ab) / root_two) - torch.erf((z_ll_ab + mu_ll_ba) / root_two)
        logits_grad *= torch.exp(-0.5 * z_perp_ab_sqr)  # l b k j

        #                 bi      lbi                               bkji
        mu_ab_sigma_g = ((scale * g).unsqueeze(-2).unsqueeze(-2) * mu_ab).sum(-1)  # l b k j
        logits_grad *= -mu_ab_sigma_g * pis.unsqueeze(-2)  # l b k j
        logits_grad = pis * sum_leftmost(logits_grad.sum(-1) / q_tot, -(1 + batch_dims))  # b k
        logits_grad *= math.sqrt(0.5 * math.pi)

        #           b j                 l b j 1   l b i             l b 1 1
        prefactor = pis.unsqueeze(-1) * q_j_prime * g.unsqueeze(-2) / q_tot_prime  # l b j i
        locs_grad = sum_leftmost(prefactor, -(2 + batch_dims))  # b j i
        scale_grad = sum_leftmost(prefactor * epsilons, -(2 + batch_dims)).sum(-2)  # b i

        return locs_grad, scale_grad, logits_grad, None, None, None
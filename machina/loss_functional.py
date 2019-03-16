"""
These are functions for loss.
Algorithms should be written by combining these functions.
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from machina.utils import detach_tensor_dict, get_device


def pg_clip(pol, batch, clip_param, ent_beta):
    """
    Policy Gradient with clipping.

    Parameters
    ----------
    pol : Pol
    batch : dict of torch.Tensor
    clip_param : float
    ent_beta : float
        entropy coefficient

    Returns
    -------
    pol_loss : torch.Tensor
    """
    obs = batch['obs']
    acs = batch['acs']
    advs = batch['advs']

    if pol.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
    else:
        h_masks = None
        out_masks = torch.ones_like(advs)

    pd = pol.pd

    old_llh = pd.llh(
        batch['acs'],
        batch,
    )

    pol.reset()
    _, _, pd_params = pol(obs, h_masks=h_masks)

    new_llh = pd.llh(acs, pd_params)
    ratio = torch.exp(new_llh - old_llh)
    pol_loss1 = - ratio * advs
    pol_loss2 = - torch.clamp(ratio, 1.0 - clip_param, 1.0 + clip_param) * advs
    pol_loss = torch.max(pol_loss1, pol_loss2)
    pol_loss = torch.mean(pol_loss * out_masks)

    ent = pd.ent(pd_params)
    pol_loss -= ent_beta * torch.mean(ent)

    return pol_loss


def pg_kl(pol, batch, kl_beta, ent_beta=0):
    """
    Policy Gradient with KL divergence restriction.

    Parameters
    ----------
    pol : Pol
    batch : dict of torch.Tensor
    kl_beta : float
        KL divergence coefficient

    Returns
    -------
    pol_loss : torch.Tensor
    """
    obs = batch['obs']
    acs = batch['acs']
    advs = batch['advs']

    if pol.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
    else:
        h_masks = None
        out_masks = torch.ones_like(advs)

    pd = pol.pd

    old_llh = pol.pd.llh(
        batch['acs'],
        batch
    )

    pol.reset()
    _, _, pd_params = pol(obs, h_masks=h_masks)

    new_llh = pol.pd.llh(acs, pd_params)
    ratio = torch.exp(new_llh - old_llh)
    pol_loss = ratio * advs * out_masks

    kl = pol.pd.kl_pq(
        batch,
        pd_params
    )

    pol_loss -= kl_beta * kl * out_masks
    pol_loss = - torch.mean(pol_loss)

    ent = pd.ent(pd_params)
    pol_loss -= ent_beta * torch.mean(ent)
    return pol_loss


def bellman(qf, targ_qf, targ_pol, batch, gamma, continuous=True, deterministic=True, sampling=1, reduction='elementwise_mean'):
    """
    Bellman loss.
    Mean Squared Error of left hand side and right hand side of Bellman Equation.

    Parameters
    ----------
    qf : SAVfunction
    targ_qf : SAVfunction
    targ_pol : Pol
    batch : dict of torch.Tensor
    gamma : float
    continuous : bool
        action space is continuous or not
    sampling : int
        Number of samping in calculating expectation.
    reduction : str
      This argument takes only elementwise, sum, and none.
      Loss shape is pytorch's manner.

    Returns
    -------
    bellman_loss : torch.Tensor
    """
    if continuous:
        obs = batch['obs']
        acs = batch['acs']
        rews = batch['rews']
        next_obs = batch['next_obs']
        dones = batch['dones']

        targ_pol.reset()
        _, _, pd_params = targ_pol(next_obs)
        pd = targ_pol.pd

        next_acs = pd.sample(pd_params, torch.Size([sampling]))
        next_obs = next_obs.expand([sampling] + list(next_obs.size()))
        targ_q, _ = targ_qf(next_obs, next_acs)
        next_q = torch.mean(targ_q, dim=0)

        targ = rews + gamma * next_q * (1 - dones)
        targ = targ.detach()
        q, _ = qf(obs, acs)

        ret = 0.5 * (q - targ)**2
        if reduction != 'none':
            ret = torch.mean(
                ret) if reduction == 'elementwise_mean' else torch.sum(ret)
        return ret
    else:
        raise NotImplementedError(
            "Only Q function with continuous action space is supported now.")


def clipped_double_bellman(qf, targ_qf1, targ_qf2, batch, gamma, loss_type='bce'):
    """
    Bellman loss of Clipped Double DQN.
    Mean Squared Error of left hand side and right hand side of Bellman Equation.
    or
    Binary Cross Entropy of left hand side and right hand side of Bellman Equation.

    Parameters
    ----------
    qf : SAVfunction
    targ_qf1 : SAVfunction
    targ_qf2 : SAVfunction
    batch : dict of torch.Tensor
    gamma : float
    loss type : str
      This argument takes only bce and mse.
      Loss shape is pytorch's manner.

    Returns
    -------
    ret : torch.Tensor
    """
    obs = batch['obs']
    acs = batch['acs']
    rews = batch['rews']
    next_obs = batch['next_obs']
    dones = batch['dones']

    targ_q1, next_acs = targ_qf1.max(next_obs)
    targ_q2, _ = targ_qf2(next_obs, next_acs)
    targ_q = torch.min(targ_q1, targ_q2)
    targ = rews + gamma * targ_q * (1 - dones)
    targ = targ.detach()
    q, _ = qf(obs, acs)
    if loss_type == 'bce':
        loss = nn.BCELoss()
        ret = loss(q, targ)
    elif loss_type == 'mse':
        ret = torch.mean(0.5 * (q - targ) ** 2)
    else:
        raise ValueError('Only bce and mse are supported')
    return ret


def sac(pol, qfs, targ_qfs, log_alpha, batch, gamma, sampling=1, reparam=True, normalize=False, eps=1e-6):
    """
    Loss for soft actor critic.

    Parameters
    ----------
    pol : Pol
    qfs : list of SAVfunction
    targ_qfs : list of SAVfunction
    log_alpha : torch.Tensor
    batch : dict of torch.Tensor
    gamma : float
    sampling : int
        Number of samping in calculating expectation.
    reparam : bool
        Reparameterization trick is used or not.
    normalize : bool
        If True, normalize value of log likelihood.
    eps : float

    Returns
    -------
    pol_loss, qf_loss, alpha_loss : torch.Tensor, torch.Tensor, torch.Tensor
    """
    obs = batch['obs']
    acs = batch['acs']
    rews = batch['rews']
    next_obs = batch['next_obs']
    dones = batch['dones']

    alpha = torch.exp(log_alpha)

    pol.reset()
    _, _, pd_params = pol(obs)
    pol.reset()
    _, _, next_pd_params = pol(next_obs)
    pd = pol.pd

    sampled_obs = obs.expand([sampling] + list(obs.size()))
    sampled_next_obs = next_obs.expand([sampling] + list(next_obs.size()))

    sampled_acs = pd.sample(pd_params, torch.Size([sampling]))
    sampled_next_acs = pd.sample(next_pd_params, torch.Size([sampling]))

    sampled_llh = pd.llh(sampled_acs.detach(), pd_params)
    sampled_next_llh = pd.llh(sampled_next_acs, next_pd_params)

    sampled_qs = [qf(sampled_obs, sampled_acs)[0] for qf in qfs]
    sampled_next_targ_qs = [targ_qf(sampled_next_obs, sampled_next_acs)[
        0] for targ_qf in targ_qfs]

    next_vs = [torch.mean(sampled_next_targ_q - alpha * sampled_next_llh, dim=0)
               for sampled_next_targ_q in sampled_next_targ_qs]
    next_v = torch.min(*next_vs)

    q_targ = rews + gamma * next_v * (1 - dones)
    q_targ = q_targ.detach()

    qs = [qf(obs, acs)[0] for qf in qfs]

    qf_losses = [0.5 * torch.mean((q - q_targ)**2) for q in qs]

    if reparam:
        pol_losses = [torch.mean(alpha * sampled_llh - sampled_q, dim=0)
                      for sampled_q in sampled_qs]
        pol_loss = torch.max(*pol_losses)
        pol_loss = torch.mean(pol_loss)
    else:
        pg_weights = [torch.mean(
            alpha * sampled_llh - sampled_q, dim=0).detach() for sampled_q in sampled_qs]
        pg_weight = torch.max(*pg_weights)

        if normalize:
            pg_weight = (pg_weight - pg_weight.mean()) / \
                (pg_weight.std() + eps)

        pol_loss = torch.mean(torch.mean(sampled_llh, dim=0) * pg_weight)

    alpha_loss = - torch.mean(log_alpha * (sampled_llh -
                                           np.prod(pol.ac_space.shape).item()).detach())

    return pol_loss, qf_losses, alpha_loss


def r2d2_sac(pol, qfs, targ_qfs, log_alpha, batch, gamma, sampling=1, burn_in_length=40, reparam=True, normalize=False, eps=1e-6):
    """
    Loss for soft actor critic.

    Parameters
    ----------
    pol : Pol
    qfs : list of SAVfunction
    targ_qfs : list of SAVfunction
    log_alpha : torch.Tensor
    batch : dict of torch.Tensor
    gamma : float
    sampling : int
        Number of samping in calculating expectation.
    reparam : bool
        Reparameterization trick is used or not.
    normalize : bool
        If True, normalize value of log likelihood.
    eps : float

    Returns
    -------
    pol_loss, qf_loss, alpha_loss : torch.Tensor, torch.Tensor, torch.Tensor
    """

    # trajectories for burn-in
    bi_obs = batch['obs'][:burn_in_length]
    bi_next_obs = batch['next_obs'][:burn_in_length]
    bi_acs = batch['acs'][:burn_in_length]
    bi_h_masks = batch['h_masks'][:burn_in_length]
    bi_next_h_masks = batch['h_masks'][1:burn_in_length+1]

    # trajectories for train
    obs = batch['obs'][burn_in_length: -1]
    acs = batch['acs'][burn_in_length: -1]
    rews = batch['rews'][burn_in_length: -1]
    next_obs = batch['next_obs'][burn_in_length: -1]
    dones = batch['dones'][burn_in_length: -1]
    h_masks = batch['h_masks'][burn_in_length: -1]
    next_h_masks = batch['h_masks'][burn_in_length+1:]

    # hidden states (time_seq, batch_size, *, cell_size)
    a_hs = (batch['hs'][0, :, 0], batch['hs'][0, :, 1])
    next_a_hs = batch['hs'][1]
    q_hs = [(batch['q_hs'+str(i)][0, :, 0], batch['q_hs'+str(i)][0, :, 1])
            for i in range(len(qfs))]
    targ_q_hs = [(batch['targ_q_hs'+str(i)][0, :, 0],
                  batch['targ_q_hs'+str(i)][0, :, 1]) for i in range(len(targ_qfs))]

    alpha = torch.exp(log_alpha)
    train_length = batch['obs'].size()[0] - burn_in_length - 1
    time_seq, batch_size, *_ = batch['obs'].size()

    pol.hs = a_hs
    with torch.no_grad():
        bi_pd_params = [pol(bi_obs[i:i+1], h_masks=bi_h_masks[i:i+1])[-1]
                        for i in range(burn_in_length)]
    pd_params = [pol(obs[i:i+1], h_masks=h_masks[i:i+1])[-1]
                 for i in range(train_length)]

    bi_next_pd_params = bi_pd_params[1:] + pd_params[0:1]
    next_pd_params = pd_params[1:] + \
        [pol(obs[-1:], h_masks=next_h_masks[-1:])[-1]]
    pd = pol.pd

    bi_sampled_obs = bi_obs.expand([sampling] + list(bi_obs.size()))
    bi_sampled_next_obs = bi_next_obs.expand(
        [sampling] + list(bi_next_obs.size()))
    sampled_obs = obs.expand([sampling] + list(obs.size()))
    sampled_next_obs = next_obs.expand([sampling] + list(next_obs.size()))

    bi_sampled_acs = torch.cat([pd.sample(bi_pd_params[i], torch.Size(
        [sampling])) for i in range(burn_in_length)], dim=1)
    bi_sampled_next_acs = torch.cat([pd.sample(bi_next_pd_params[i], torch.Size(
        [sampling])) for i in range(burn_in_length)], dim=1)
    sampled_acs = torch.cat([pd.sample(pd_params[i], torch.Size(
        [sampling])) for i in range(train_length)], dim=1)
    sampled_next_acs = torch.cat([pd.sample(next_pd_params[i], torch.Size(
        [sampling])) for i in range(train_length)], dim=1)

    sampled_llh = torch.cat(
        [pd.llh(sampled_acs[0][i].detach(), pd_params[i]) for i in range(train_length)])
    sampled_next_llh = torch.cat(
        [pd.llh(sampled_next_acs[0][i], next_pd_params[i]) for i in range(train_length)])

    for i in range(len(qfs)):
        qfs[i].hs = q_hs[i]
        targ_qfs[i].hs = targ_q_hs[i]

    # forward of qfs and targ_qfs for burn-in
    with torch.no_grad():
        _ = [[qf(bi_sampled_obs[i], bi_sampled_acs[i], hs=q_hs[i],
                 h_masks=bi_h_masks) for i in range(sampling)] for qf in qfs]
        _ = [[targ_qf(bi_sampled_next_obs[i], bi_sampled_next_acs[i], hs=targ_q_hs[i],
                      h_masks=bi_next_h_masks) for i in range(sampling)] for targ_qf in targ_qfs]

    # forward of qfs and targ_qfs for train
    sampled_qs = torch.cat([torch.cat([qf(sampled_obs[i], sampled_acs[i], h_masks=h_masks)[
                           0].unsqueeze(0) for i in range(sampling)]).unsqueeze(0) for qf in qfs])
    sampled_next_targ_qs = torch.cat([torch.cat([targ_qf(sampled_next_obs[i], sampled_next_acs[i], h_masks=next_h_masks)[
        0].unsqueeze(0) for i in range(sampling)]).unsqueeze(0) for targ_qf in targ_qfs])

    next_vs = torch.cat([torch.mean(sampled_next_targ_q - alpha * sampled_next_llh, dim=0).unsqueeze(0)
                         for sampled_next_targ_q in sampled_next_targ_qs])

    next_vs = torch.min(next_vs, dim=0)[0]

    q_targ = rews + gamma * next_vs * (1 - dones)
    q_targ = q_targ.detach()

    for i in range(len(qfs)):
        qfs[i].hs = q_hs[i]
    with torch.no_grad():
        _ = [qf(bi_obs, bi_acs, h_masks=bi_h_masks)[0] for qf in qfs]
    qs = [qf(obs, acs, h_masks=h_masks)[0] for qf in qfs]

    td_losses = [(q - q_targ) for q in qs]
    qf_losses = [0.5 * torch.mean((td_loss)**2) for td_loss in td_losses]
    td_losses = torch.cat([td_loss.unsqueeze(0) for td_loss in td_losses])
    td_losses = torch.mean(td_losses, dim=0)

    if reparam:
        pol_losses = [torch.mean(torch.mean(alpha * sampled_llh - sampled_q, dim=0), dim=0)
                      for sampled_q in sampled_qs]
        pol_loss = torch.max(*pol_losses)
        pol_loss = torch.mean(pol_loss)
    else:
        pg_weights = [torch.mean(torch.mean(
            alpha * sampled_llh - sampled_q, dim=0), dim=0).detach() for sampled_q in sampled_qs]
        pg_weight = torch.max(*pg_weights)

        if normalize:
            pg_weight = (pg_weight - pg_weight.mean()) / \
                (pg_weight.std() + eps)

        pol_loss = torch.mean(torch.mean(torch.mean(
            sampled_llh, dim=0), dim=0) * pg_weight)

    alpha_loss = - torch.mean(log_alpha * (sampled_llh -
                                           np.prod(pol.ac_space.shape).item()).detach())
    return batch, pol_loss, qf_losses, alpha_loss, td_losses


def ag(pol, qf, batch, sampling=1, no_noise=False):
    """
    DDPG style action gradient.

    Parameters
    ----------
    pol : Pol
    qf : SAVfunction
    batch : dict of torch.Tensor
    sampling : int
        Number of samping in calculating expectation.

    Returns
    -------
    pol_loss : torch.Tensor
    """
    obs = batch['obs']

    if not no_noise:
        _, _, pd_params = pol(obs)
    else:
        _, _, pd_params = pol(obs, no_noise)

    pd = pol.pd

    acs = pd.sample(pd_params, torch.Size([sampling]))
    q, _ = qf(obs.expand([sampling] + list(obs.size())), acs)
    q = torch.mean(q, dim=0)

    pol_loss = - torch.mean(q)

    return pol_loss


def pg(pol, batch, ent_beta=0):
    """
    Policy Gradient.

    Parameters
    ----------
    pol : Pol
    batch : dict of torch.Tensor

    Returns
    -------
    pol_loss : torch.Tensor
    """
    obs = batch['obs']
    acs = batch['acs']
    advs = batch['advs']

    pd = pol.pd
    pol.reset()
    if pol.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
        _, _, pd_params = pol(obs, h_masks=h_masks)
    else:
        out_masks = torch.ones_like(advs)
        _, _, pd_params = pol(obs)

    llh = pol.pd.llh(acs, pd_params)

    pol_loss = - torch.mean(llh * advs * out_masks)
    ent = pd.ent(pd_params)
    pol_loss -= ent_beta * torch.mean(ent)
    return pol_loss


def monte_carlo(vf, batch, clip_param=0.2, clip=False):
    """
    Montecarlo loss for V function.

    Parameters
    ----------
    vf : SVfunction
    batch : dict of torch.Tensor
    clip_param : float
    clip : bool

    Returns
    -------

    """
    obs = batch['obs']
    rets = batch['rets']

    vf.reset()
    if vf.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
        vs, _ = vf(obs, h_masks=h_masks)
    else:
        out_masks = torch.ones_like(rets)
        vs, _ = vf(obs)

    vfloss1 = (vs - rets)**2
    if clip:
        old_vs = batch['vs']
        vpredclipped = old_vs + \
            torch.clamp(vs - old_vs, -clip_param, clip_param)
        vfloss2 = (vpredclipped - rets)**2
        vf_loss = 0.5 * torch.mean(torch.max(vfloss1, vfloss2) * out_masks)
    else:
        vf_loss = 0.5 * torch.mean(vfloss1 * out_masks)
    return vf_loss


def dynamics(dm, batch, target='next_obs', td=True):
    """
    MSE loss for Dynamics models.
    Parameters
    ----------
    dm : Dynamics Model
    batch : dict of torch.Tensor
    target : string
        Prediction target is next_obs or rews.
    td : bool
        If True, the dynamics model learn temporal difference of dynamics.

    Returns
    -------
    model_loss : torch.Tensor
    """

    obs = batch['obs']
    acs = batch['acs']

    dm.reset()
    if dm.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
        pred, _ = dm(obs, acs, h_masks=h_masks)
    else:
        out_masks = torch.ones(
            obs.size()[0], dtype=torch.float, device=get_device())
        pred, _ = dm(obs, acs)

    if target == 'rews' or not td:
        dm_loss = (pred - batch[target])**2
    else:
        dm_loss = (pred - (batch['next_obs'] - batch['obs']))**2
    dm_loss = 0.5 * torch.mean(torch.mean(dm_loss, dim=-1) * out_masks)

    return dm_loss


def log_likelihood(pol, batch):
    obs = batch['obs']
    acs = batch['acs']
    _, _, pd_params = pol(obs)
    llh = pol.pd.llh(acs, pd_params)
    pol_loss = -torch.mean(llh)
    return pol_loss


def cross_ent(discrim, batch, expert_or_agent, ent_beta):
    obs = batch['obs']
    acs = batch['acs']
    len = obs.shape[0]
    logits, _ = discrim(obs, acs)
    discrim_loss = F.binary_cross_entropy_with_logits(
        logits, torch.ones(len, device=get_device())*expert_or_agent)
    ent = (1 - torch.sigmoid(logits))*logits - F.logsigmoid(logits)
    discrim_loss -= ent_beta * torch.mean(ent)
    return discrim_loss


def density_ratio_rew_cross_ent(rewf, shaping_vf, pol, batch, expert_or_agent, gamma):
    obs = batch['obs']
    acs = batch['acs']
    next_obs = batch['next_obs']
    dones = batch['dones']
    vs, _ = shaping_vf(obs)
    rews, _ = rewf(obs)
    next_vs, _ = shaping_vf(next_obs)
    energies = rews + (1 - dones) * gamma * next_vs - vs
    with torch.no_grad():
        _, _, params = pol(obs)
        llhs = pol.pd.llh(acs, params)
    logits = energies - llhs
    len = obs.shape[0]
    discrim_loss = F.binary_cross_entropy_with_logits(
        logits, torch.ones(len, device=get_device())*expert_or_agent)
    return discrim_loss


def density_ratio_adv_cross_ent(advf, pol, batch, expert_or_agent):
    obs = batch['obs']
    acs = batch['acs']
    advs, _ = advf(obs, acs)
    energies = advs
    with torch.no_grad():
        _, _, params = pol(obs)
        llhs = pol.pd.llh(acs, params)
    logits = energies - llhs
    len = obs.shape[0]
    discrim_loss = F.binary_cross_entropy_with_logits(
        logits, torch.ones(len, device=get_device())*expert_or_agent)
    return discrim_loss


def shannon_cross_entropy(student_pol, teacher_pol, batch):
    """
    Shannon-cross-entropy for policy distillation
    See https://arxiv.org/abs/1902.02186

    Parameters
    ----------
    student_pol : Student Policy
    teacher_pol : Teacher Policy
    batch : data containing the observations, actions and rewards

    Returns
    -------
    cross_entropy_loss : Cross entropy loss between teacher and student policy
    """

    obs = batch['obs']
    if teacher_pol.rnn:
        h_masks = batch['h_masks']
        out_masks = batch['out_masks']
    else:
        h_masks = None
        out_masks = torch.ones_like(batch['rews'])
    s_pd = student_pol.pd
    student_pol.reset()
    teacher_pol.reset()
    _, _, s_params = student_pol(obs, h_masks=h_masks)
    with torch.no_grad():
        _, _, t_params = teacher_pol(obs, h_masks=h_masks)
    cross_entropy_loss = s_pd.kl_pq(t_params, s_params) - s_pd.ent(t_params)
    return torch.mean(cross_entropy_loss)

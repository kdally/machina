"""
Gaussian State Dynamics Model
"""

from machina.models.base import BaseModel
from machina.utils import get_device


class GaussianSModel(BaseModel):
    """
    Gaussian version of State Dynamics Model.

    Parameters
    ----------
    ob_space : gym.Space
    ac_space : gym.Space
    net : torch.nn.Module
    rnn : bool
    data_parallel : bool
        If True, network computation is executed in parallel.
    parallel_dim : int
        Splitted dimension in data parallel.
    """

    def __init__(self, ob_space, ac_space, net, rnn=False, data_parallel=False, parallel_dim=0):
        super().__init__(ob_space, ac_space, net, rnn, data_parallel, parallel_dim)
        self.pd = GaussianPd()
        self.to(get_device())

    def forward(self, obs, acs, hs=None, h_masks=None):
        obs = self._check_obs_shape(obs)
        acs = self._check_acs_shape(acs)

        if self.rnn:
            time_seq, batch_size, *_ = obs.shape

            if hs is None:
                if self.hs is None:
                    self.hs = self.net.init_hs(batch_size)
                hs = self.hs

            if h_masks is None:
                h_masks = hs[0].new(time_seq, batch_size, 1).zero_()
            h_masks = h_masks.reshape(time_seq, batch_size, 1)

            mean, log_std, hs = self.net(obs, acs, hs, h_masks)
            self.hs = hs
        else:
            mean, log_std = self.net(obs, acs)
        log_std = log_std.expand_as(mean)
        d_ob = self.pd.sample(dict(mean=mean, log_std=log_std))
        return d_ob, dict(mean=mean)
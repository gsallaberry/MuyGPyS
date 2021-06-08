# Copyright 2021 Lawrence Livermore National Security, LLC and other MuyGPyS
# Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import numpy as np

# from sklearn.gaussian_process.kernels import Matern, RBF

# from MuyGPyS.gp.kernels import NNGPimpl as NNGP
from MuyGPyS.gp.kernels import (
    Matern,
    RBF,
    NNGP,
    _get_kernel,
    _init_hyperparameter,
)


class MuyGPS:
    """
    Local Kriging Gaussian Process.

    Performs approximate GP inference by locally approximating an observation's
    response using its nearest neighbors.
    """

    def __init__(
        self,
        kern="matern",
        metric="l2",
        eps={"val": 1e-5},
        sigma_sq=[{"val": 1e0}],
        **kwargs,
    ):
        """
        Initialize.

        Parameters
        ----------
        kern : str
            The kernel to be used. Each kernel supports different
            hyperparameters that can be specified in kwargs.
            NOTE[bwp] Currently supports ``matern'', ``rbf'' and ``nngp''.
        """
        self.kern = kern.lower()
        self.metric = metric.lower()
        self.kernel = _get_kernel(self.kern, **kwargs)
        self.eps = _init_hyperparameter(1e-14, "fixed", **eps)
        self.sigma_sq = [
            _init_hyperparameter(1.0, "fixed", **ss) for ss in sigma_sq
        ]

    def get_optim_params(self):
        optim_params = {
            p: self.kernel.hyperparameters[p]
            for p in self.kernel.hyperparameters
            if self.kernel.hyperparameters[p].get_bounds() != "fixed"
        }
        if self.eps.get_bounds() != "fixed":
            optim_params["eps"] = self.eps
        return optim_params

    def _compute_solve(self, K, Kcross, batch_targets):
        """
        Simultaneously solve all of the GP inference systems of linear
        equations.

        Parameters
        ----------
        K : np.ndarray(float), shape = ``(batch_size, nn_count, nn_count)''
            A tensor containing the ``nn_count'' x ``nn_count'' kernel matrices
            corresponding to each of the batch elements.
        Kcross : np.ndarray(float), shape = ``(batch_size, nn_count)''
            A tensor containing the 1 x ``nn_count'' cross-covariance matrix
            corresponding to each of the batch elements.
        batch_targets : numpy.ndarray(float),
                  shape = ``(batch_size, nn_count, response_count)''
            The vector-valued responses for the nearest neighbors of each
            batch element.

        Returns
        -------
        numpy.ndarray(float), shape = ``(batch_count, response_count)''
            The predicted response for each of the given indices.
        """
        batch_size, nn_count, response_count = batch_targets.shape
        responses = Kcross.reshape(batch_size, 1, nn_count) @ np.linalg.solve(
            K + self.eps() * np.eye(nn_count), batch_targets
        )
        return responses.reshape(batch_size, response_count)

    def _compute_diagonal_variance(self, K, Kcross):
        """
        Simultaneously solve all of the GP inference systems of linear
        equations.

        Parameters
        ----------
        K : np.ndarray(float), shape = ``(batch_size, nn_count, nn_count)''
            A tensor containing the ``nn_count'' x ``nn_count'' kernel matrices
            corresponding to each of the batch elements.
        Kcross : np.ndarray(float), shape = ``(batch_size, nn_count)''
            A tensor containing the 1 x ``nn_count'' cross-covariance matrix
            corresponding to each of the batch elements.

        Returns
        -------
        numpy.ndarray(float), shape = ``(batch_count, response_count,)''
            The predicted response for each of the given indices.
        """
        batch_size, nn_count = Kcross.shape
        return np.array(
            [
                1.0
                - Kcross[i, :]
                @ np.linalg.solve(
                    K[i, :, :] + self.eps() * np.eye(nn_count), Kcross[i, :]
                )
                for i in range(batch_size)
            ]
        )

    def regress(
        self,
        K,
        Kcross,
        batch_targets,
        variance_mode=None,
    ):
        """
        Performs simultaneous regression on a list of observations.

        Parameters
        ----------
        K : np.ndarray(float), shape = ``(batch_size, nn_count, nn_count)''
            A tensor containing the ``nn_count'' x ``nn_count'' kernel matrices
            corresponding to each of the batch elements.
        Kcross : np.ndarray(float), shape = ``(batch_size, nn_count)''
            A tensor containing the 1 x ``nn_count'' cross-covariance matrix
            corresponding to each of the batch elements.
        batch_targets : numpy.ndarray(float),
                  shape = ``(batch_size, nn_count, response_count)''
            The vector-valued responses for the nearest neighbors of each
            batch element.
        variance_mode : str or None
            Specifies the type of variance to return. Currently supports
            ``diagonal'' and None. If None, report no variance term.

        Returns
        -------
        responses : numpy.ndarray(float),
                    shape = ``(batch_count, response_count,)''
            The predicted response for each of the given indices.
        diagonal_variance : numpy.ndarray(float), shape = ``(batch_count,)
            The diagonal elements of the posterior variance. Only returned where
            ``variance_mode == "diagonal"''.
        """
        responses = self._compute_solve(K, Kcross, batch_targets)
        if variance_mode is None:
            return responses
        elif variance_mode == "diagonal":
            diagonal_variance = self._compute_diagonal_variance(K, Kcross)
            return responses, diagonal_variance
        else:
            raise NotImplementedError(
                f"Variance mode {variance_mode} is not implemented."
            )

    def sigma_sq_optim(
        self,
        K,
        nn_indices,
        targets,
    ):
        """
        Optimize the value of the sigma^2 scale parameter for each response
        dimension.

        We approximate sigma^2 by way of averaging over the analytic solution
        from each local kernel.

        sigma^2 = 1/n * Y^T @ K^{-1} @ Y

        Parameters
        ----------
        index : np.ndarray(int), shape = ``(batch_count,)''
            The integer indices of the observations to be approximated.
        nn_indices : numpy.ndarray(int), shape = ``(batch_size, nn_count)''
            A matrix listing the nearest neighbor indices for all observations
            in the testing batch.
        targets : numpy.ndarray(float),
                  shape = ``(train_count, response_count)''
            Vector-valued responses for each training element.

        Returns
        -------
        sigmas : numpy.ndarray(float), shape = ``(response_count,)''
            The value of sigma^2 for each dimension.
        """
        batch_size, nn_count = nn_indices.shape
        _, response_count = targets.shape

        for i in range(response_count):
            self.sigma_sq[i]._set_val(
                sum(self._get_sigma_sq(K, targets[:, i], nn_indices))
                / (nn_count * batch_size)
            )

    def _get_sigma_sq_series(
        self,
        K,
        nn_indices,
        target_col,
    ):
        """
        Return the series of sigma^2 scale parameters for each neighborhood
        solve.
        NOTE[bwp]: This function is only for testing purposes.

        Parameters
        ----------
        index : np.ndarray(int), shape = ``(batch_count,)''
            The integer indices of the observations to be approximated.
        nn_indices : numpy.ndarray(int), shape = ``(batch_size, nn_count)''
            A matrix listing the nearest neighbor indices for all observations
            in the testing batch.
        train : numpy.ndarray(float), shape = ``(train_count, feature_count)''
            The full training data matrix.
        target_col : numpy.ndarray(float), shape = ``(train_count,)''
            The target vector consisting of the target for each nearest
            neighbor.

        Returns
        -------
        sigmas : numpy.ndarray(float), shape = ``(response_count,)''
            The value of sigma^2 for each dimension.
        """
        batch_size, nn_count = nn_indices.shape

        sigmas = np.zeros((batch_size,))
        for i, el in enumerate(self._get_sigma_sq(K, target_col, nn_indices)):
            sigmas[i] = el
        return sigmas / nn_count

    def _get_sigma_sq(self, K, target_col, nn_indices):
        """
        Generate series of sigma^2 scale parameters for each individual solve
        along a single dimension:

        sigma^2 = 1/nn * Y_{nn}^T @ K_{nn}^{-1} @ Y_{nn}

        Parameters
        ----------
        K : np.ndarray(float), shape = ``(batch_count, nn_count, nn_count)''
            Kernel tensor containing nearest neighbor kernels for each local
            neighborhood.
        target_col : numpy.ndarray(float), shape = ``(batch_count,)''
            The target vector consisting of the target for each nearest
            neighbor.
        nn_indices : numpy.ndarray(int), shape = ``(batch_size, nn_count)''
            A matrix listing the nearest neighbor indices for all observations
            in the testing batch.

        Yields
        -------
        sigmas : numpy.ndarray(float), shape = ``(batch_count,)''
            The optimal value of sigma^2 for each neighborhood for the given
            output dimension.
        """
        batch_size, nn_count = nn_indices.shape
        for j in range(batch_size):
            Y_0 = target_col[nn_indices[j, :]]
            yield Y_0 @ np.linalg.solve(
                K[j, :, :] + self.eps() * np.eye(nn_count), Y_0
            )

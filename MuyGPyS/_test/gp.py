# Copyright 2021-2023 Lawrence Livermore National Security, LLC and other
# MuyGPyS Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: MIT

from copy import deepcopy
from typing import Dict, Tuple, Union

from sklearn.metrics import pairwise_distances as skl_pairwise_distances

import MuyGPyS._src.math.numpy as np
from MuyGPyS.gp.distortion import NullDistortion
from MuyGPyS.gp.kernels import Hyperparameter, Matern
from MuyGPyS.gp.noise import HeteroscedasticNoise, HomoscedasticNoise, NullNoise
from MuyGPyS.gp.sigma_sq import SigmaSq


def benchmark_select_skl_metric(metric: str) -> str:
    """
    Convert `MuyGPyS` metric names to `scikit-learn` equivalents.

    Args:
        metric:
            The `MuyGPyS` name of the metric.

    Returns:
        The equivalent `scikit-learn` name.

    Raises:
        ValueError:
            Any value other than `"l2"` or `"F2"` will produce an error.
    """
    if metric == "l2":
        return "l2"
    elif metric == "F2":
        return "sqeuclidean"
    else:
        raise ValueError(f"Metric {metric} is not supported!")


def benchmark_pairwise_distances(data, metric: str = "l2"):
    """
    Create a matrix of pairwise distances among a dataset.

    Takes a full dataset of records of interest `data` and produces distances
    between each pair of elements in a square matrix.

    Args:
        data:
            The data matrix of shape `(data_count, feature_count)` containing
            batch elements.
        metric:
            The name of the metric to use in order to form distances. Supported
            values are `l2` and `F2`.

    Returns:
        A square matrix of shape `(data_count, data_count,)` containing the
        pairwise distances between the nearest neighbors of the batch elements.
    """
    return skl_pairwise_distances(
        data, metric=benchmark_select_skl_metric(metric)
    )


def benchmark_crosswise_distances(test, train, metric: str = "l2"):
    """
    Create a matrix of crosswise distances between a test and train set.

    Takes two full datasets of records of interest `test` and `train` and
    produces distances between each pair of `test` and `train` elements in a
    matrix.

    Args:
        test:
            The data matrix of shape `(test_count, feature_count)` containing
            test elements.
        train:
            The data matrix of shape `(train_count, feature_count)` containing
            train elements.
        metric:
            The name of the metric to use in order to form distances. Supported
            values are `l2` and `F2`.

    Returns:
        A matrix of shape `(test_count, train_count,)` containing the crosswise
        distances between pairs of train and test elements.
    """
    return skl_pairwise_distances(
        test, train, metric=benchmark_select_skl_metric(metric)
    )


class BenchmarkGP:
    """
    A basic Gaussian Process.

    Performs GP inference and simulation by way of analytic computations.

    Args:
        kernel:
            The kernel to be used. Only supports Matern.
        eps:
            The noise model.
    """

    def __init__(
        self,
        kernel: Matern,
        eps: Union[
            HeteroscedasticNoise, HomoscedasticNoise, NullNoise
        ] = HomoscedasticNoise(0.0),
    ):
        """
        Initialize.
        """
        self.kernel = kernel
        if not isinstance(self.kernel._distortion_fn, NullDistortion):
            raise ValueError(
                f"BenchmarkGP must use NullDistortion, not "
                f"{type(self.kernel._distortion_fn)}"
            )
        # only supporting l2/Matern
        self.metric = self.kernel._distortion_fn.metric
        self.eps = eps
        self.sigma_sq = SigmaSq()

    def set_eps(self, **eps) -> None:
        """
        Reset :math:`\\varepsilon` value or bounds.

        Uses existing value and bounds as defaults.

        Args:
            eps:
                A hyperparameter dict.
        """
        self.eps._set(**eps)

    def _set_sigma_sq(self, val) -> None:
        """
        Reset :math:`\\varepsilon` value or bounds.

        This is dangerous to do in general, and it only included for testing
        purposes. Make sure you know what you are doing before invoking! Uses
        existing value and bounds as defaults.

        Args:
            val:
                A scalar value for `sigma_sq`.
        """
        self.sigma_sq._set(val)

    def fixed(self) -> bool:
        """
        Checks whether all kernel and model parameters are fixed.

        This is a convenience utility to determine whether optimization is
        required.

        Returns:
            Returns `True` if all parameters are fixed, and `False` otherwise.
        """
        for p in self.kernel.hyperparameters:
            if not self.kernel.hyperparameters[p].fixed():
                return False
        if not self.eps.fixed():
            return False
        return True

    def get_optim_params(self) -> Dict[str, Hyperparameter]:
        """
        Return a dictionary of references to the unfixed kernel hyperparameters.

        This is a convenience function for obtaining all of the information
        necessary to optimize hyperparameters. It is important to note that the
        values of the dictionary are references to the actual hyperparameter
        objects underying the kernel functor - changing these references will
        change the kernel.

        Returns:
            A dict mapping hyperparameter names to references to their objects.
            Only returns hyperparameters whose bounds are not set as `fixed`.
            Returned hyperparameters can include `eps`, but not `sigma_sq`,
            as it is currently optimized via a separate closed-form method.
        """
        optim_params = {
            p: self.kernel.hyperparameters[p]
            for p in self.kernel.hyperparameters
            if not self.kernel.hyperparameters[p].fixed()
        }
        if not self.eps.fixed():
            optim_params["eps"] = self.eps
        return optim_params

    def regress(
        self,
        test: np.ndarray,
        train: np.ndarray,
        targets: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Performs simultaneous regression on a list of observations.

        Args:
            test:
                The full testing data matrix of shape
                `(test_count, feature_count)`.
            train:
                The full training data matrix of shape
                `(train_count, feature_count)`.
            targets:
                A matrix of shape `(train_count, ouput_dim)` whose rows consist
                of vector-valued responses for each training element.

        Returns
        -------
        responses:
            A matrix of shape `(batch_count, response_count)` whose rows consist
            of the predicted response for each of the given indices.
        diagonal_variance:
            A vector of shape `(batch_count,)` consisting of the diagonal
            elements of the posterior variance.
        """
        crosswise_dists = benchmark_crosswise_distances(
            test, train, metric=self.metric
        )
        pairwise_dists = benchmark_pairwise_distances(train, metric=self.metric)
        Kcross = self.kernel(crosswise_dists)
        K = self.kernel(pairwise_dists)
        responses = Kcross @ np.linalg.solve(K, targets)

        test_pairwise_dists = benchmark_pairwise_distances(
            test, metric=self.metric
        )
        Kstar = self.kernel(test_pairwise_dists)
        variance = Kstar - Kcross @ np.linalg.solve(K, Kcross.T)
        return responses, variance


def benchmark_sample_full(
    gp: BenchmarkGP,
    test: np.ndarray,
    train: np.ndarray,
) -> np.ndarray:
    """
    Sample from a GP prior for a dataset separated into train and test.

    Args:
        gp:
            The gp object
        test:
            The full testing data matrix of shape
            `(test_count, feature_count)`.
        train:
            The full training data matrix of shape
            `(train_count, feature_count)`.

    Returns:
        A sample from the GP prior for a train/test split.
    """
    return benchmark_sample(gp, np.vstack((test, train)))


def benchmark_prepare_cholK(
    gp: BenchmarkGP,
    data: np.ndarray,
) -> np.ndarray:
    """
    Sample from a GP prior for a dataset.

    Args:
        gp:
            The gp object
        train:
            The full training data matrix of shape
            `(train_count, feature_count)`.

    Returns:
        The Cholesky decomposition of a dense covariance matrix.
    """
    pairwise_dists = benchmark_pairwise_distances(data, metric=gp.metric)
    data_count, _ = data.shape
    Kfull = gp.sigma_sq()[0] * (
        gp.kernel(pairwise_dists) + gp.eps() * np.eye(data_count)
    )
    return np.linalg.cholesky(Kfull)


def benchmark_sample(
    gp: BenchmarkGP,
    data: np.ndarray,
) -> np.ndarray:
    """
    Sample from a GP prior for a dataset.

    Args:
        gp:
            The gp object
        train:
            The full training data matrix of shape
            `(train_count, feature_count)`.
    """
    cholK = benchmark_prepare_cholK(gp, data)
    return benchmark_sample_from_cholK(cholK)


def benchmark_sample_from_cholK(cholK: np.ndarray) -> np.ndarray:
    data_count, _ = cholK.shape
    return (
        cholK @ np.array(np.random.normal(0, 1, size=(data_count,)))
    ).reshape(data_count, 1)


def get_analytic_sigma_sq(K, y):
    assert y.shape[0] == K.shape[0]
    return (1 / y.shape[0]) * y.T @ np.linalg.solve(K, y)

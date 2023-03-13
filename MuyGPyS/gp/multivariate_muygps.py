# Copyright 2021-2023 Lawrence Livermore National Security, LLC and other
# MuyGPyS Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: MIT

"""
Multivariate MuyGPs implementation
"""

from typing import List, Optional, Tuple, Union

import MuyGPyS._src.math as mm
from MuyGPyS._src.gp.distance import (
    _make_regress_tensors,
    _make_fast_regress_tensors,
)
from MuyGPyS._src.gp.distance.numpy import (
    _make_regress_tensors as _make_regress_tensors_n,
)
from MuyGPyS._src.gp.muygps import (
    _muygps_fast_regress_precompute,
    _mmuygps_fast_regress_solve,
)
from MuyGPyS._src.gp.noise import _homoscedastic_perturb
from MuyGPyS._src.mpi_utils import _is_mpi_mode
from MuyGPyS.gp.distance import crosswise_distances
from MuyGPyS.gp.kernels import SigmaSq
from MuyGPyS.gp.muygps import MuyGPS


class MultivariateMuyGPS:
    """
    Multivariate Local Kriging Gaussian Process.

    Performs approximate GP inference by locally approximating an observation's
    response using its nearest neighbors with a separate kernel allocated for
    each response dimension, implemented as individual
    :class:`MuyGPyS.gp.muygps.MuyGPS` objects.

    This class is similar in interface to :class:`MuyGPyS.gp.muygps.MuyGPS`, but
    requires a list of hyperparameter dicts at initialization.

    Example:
        >>> from MuyGPyS.gp import MultivariateMuyGPS as MMuyGPS
        >>> k_kwargs1 = {
        ...         "eps": {"val": 1e-5},
        ...         "nu": {"val": 0.67, "bounds": (0.1, 2.5)},
        ...         "length_scale": {"val": 7.2},
        ... }
        >>> k_kwargs2 = {
        ...         "eps": {"val": 1e-5},
        ...         "nu": {"val": 0.38, "bounds": (0.1, 2.5)},
        ...         "length_scale": {"val": 7.2},
        ... }
        >>> k_args = [k_kwargs1, k_kwargs2]
        >>> mmuygps = MMuyGPS("matern", *k_args)

    We can realize kernel tensors for each of the models contained within a
    `MultivariateMuyGPS` object by iterating over its `models` member. Once we
    have computed a `pairwise_dists` tensor and a `crosswise_dists` matrix, it
    is straightforward to perform each of these realizations.

    Example:
        >>> for model in MuyGPyS.models:
        >>>         K = model.kernel(pairwise_dists)
        >>>         Kcross = model.kernel(crosswise_dists)
        >>>         # do something with K and Kcross...

    Args
        kern:
            The kernel to be used. Each kernel supports different
            hyperparameters that can be specified in kwargs. Currently supports
            only `matern` and `rbf`.
        model_args:
            Dictionaries defining each internal
            :class:`MuyGPyS.gp.muygps.MuyGPS` instance.
    """

    def __init__(
        self,
        kern: str,
        *model_args,
    ):
        self.kern = kern.lower()
        self.models = [MuyGPS(kern, **args) for args in model_args]
        self.metric = self.models[0].kernel.metric  # this is brittle
        self.sigma_sq = SigmaSq(len(self.models))

    def fixed(self) -> bool:
        """
        Checks whether all kernel and model parameters are fixed for each model,
        excluding :math:`\\sigma^2`.

        Returns:
            Returns `True` if all parameters in all models are fixed, and
            `False` otherwise.
        """
        return bool(all([model.fixed() for model in self.models]))

    def regress_from_indices(
        self,
        indices: mm.ndarray,
        nn_indices: mm.ndarray,
        test: mm.ndarray,
        train: mm.ndarray,
        targets: mm.ndarray,
        variance_mode: Optional[str] = None,
        apply_sigma_sq: bool = True,
        return_distances: bool = False,
        indices_by_rank: bool = False,
    ) -> Union[
        mm.ndarray,
        Tuple[mm.ndarray, mm.ndarray],
        Tuple[mm.ndarray, mm.ndarray, mm.ndarray],
        Tuple[mm.ndarray, mm.ndarray, mm.ndarray, mm.ndarray],
    ]:
        """
        Performs simultaneous regression on a list of observations.

        Implicitly creates and discards the distance tensors and matrices. If
        these data structures are needed for later reference, instead use
        :func:`~MuyGPyS.gp.muygps.MultivariateMuyGPS.regress`.

        Args:
            indices:
                An integral vector of shape `(batch_count,)` indices of the
                observations to be approximated.
            nn_indices:
                An integral matrix of shape `(batch_count, nn_count)` listing the
                nearest neighbor indices for all observations in the test batch.
            test:
                The full testing data matrix of shape
                `(test_count, feature_count)`.
            train:
                The full training data matrix of shape
                `(train_count, feature_count)`.
            targets:
                A matrix of shape `(train_count, response_count)` whose rows are
                vector-valued responses for each training element.
            variance_mode:
                Specifies the type of variance to return. Currently supports
                `"diagonal"` and None. If None, report no variance term.
            apply_sigma_sq:
                Indicates whether to scale the posterior variance by `sigma_sq`.
                Unused if `variance_mode is None` or
                `sigma_sq.trained is False`.
            return_distances:
                If `True`, returns a `(test_count, nn_count)` matrix containing
                the crosswise distances between the test elements and their
                nearest neighbor sets and a `(test_count, nn_count, nn_count)`
                tensor containing the pairwise distances between the test data's
                nearest neighbor sets.
            indices_by_rank:
                If `True`, construct the tensors using local indices with no
                communication. Only for use in MPI mode.
        Returns
        -------
        responses:
            A matrix of shape `(batch_count, response_count)` whose rows are
            the predicted response for each of the given indices.
        variance:
            A vector of shape `(batch_count,)` consisting of the diagonal
            elements of the posterior variance. Only returned where
            `variance_mode == "diagonal"`.
        crosswise_dists:
            A matrix of shape `(test_count, nn_count)` whose rows list the
            distance of the corresponding test element to each of its nearest
            neighbors. Only returned if `return_distances is True`.
        pairwise_dists:
            A tensor of shape `(test_count, nn_count, nn_count)` whose latter
            two dimensions contain square matrices containing the pairwise
            distances between the nearest neighbors of the test elements. Only
            returned if `return_distances is True`.
        """
        tensor_fn = (
            _make_regress_tensors_n
            if _is_mpi_mode() is True and indices_by_rank is True
            else _make_regress_tensors
        )

        (crosswise_dists, pairwise_dists, batch_nn_targets,) = tensor_fn(
            self.metric,
            indices,
            nn_indices,
            test,
            train,
            targets,
        )
        responses = self.regress(
            pairwise_dists,
            crosswise_dists,
            batch_nn_targets,
            variance_mode=variance_mode,
            apply_sigma_sq=apply_sigma_sq,
        )
        if return_distances is False:
            return responses
        else:
            if variance_mode is None:
                return responses, crosswise_dists, pairwise_dists
            else:
                responses, variances = responses
                return responses, variances, crosswise_dists, pairwise_dists

    def regress(
        self,
        pairwise_dists: mm.ndarray,
        crosswise_dists: mm.ndarray,
        batch_nn_targets: mm.ndarray,
        variance_mode: Optional[str] = None,
        apply_sigma_sq: bool = True,
    ) -> Union[mm.ndarray, Tuple[mm.ndarray, mm.ndarray]]:
        """
        Performs simultaneous regression on provided distance tensors and
        the target matrix.

        Computes parallelized local solves of systems of linear equations using
        the kernel realizations, one for each internal model, of the last two
        dimensions of `pairwise_dists` along with `crosswise_dists` and
        `batch_nn_targets` to predict responses in terms of the posterior mean.
        Also computes the posterior variance if `variance_mode` is set
        appropriately. Assumes that distance tensor `pairwise_dists` and
        crosswise distance matrix `crosswise_dists` are already computed and
        given as arguments. To implicitly construct these values from indices
        (useful if the distance tensors and matrices are not needed for later
        reference) instead use
        :func:`~MuyGPyS.gp.muygps.MultivariateMuyGPS.regress_from_indices`.

        Returns the predicted response in the form of a posterior
        mean for each element of the batch of observations by solving a system
        of linear equations induced by each kernel functor, one per response
        dimension, in a generalization of Equation (3.4) of
        [muyskens2021muygps]_. For each batch element :math:`\\mathbf{x}_i` we
        compute

        .. math::
            \\widehat{Y}_{NN} (\\mathbf{x}_i \\mid X_{N_i})_{:,j} =
                K^{(j)}_\\theta (\\mathbf{x}_i, X_{N_i})
                (K^{(j)}_\\theta (X_{N_i}, X_{N_i}) + \\varepsilon_j I_k)^{-1}
                Y(X_{N_i})_{:,j}.

        Here :math:`X_{N_i}` is the set of nearest neighbors of
        :math:`\\mathbf{x}_i` in the training data, :math:`K^{(j)}_\\theta` is
        the kernel functor associated with the jth internal model, corresponding
        to the jth response dimension, :math:`\\varepsilon_j I_k` is a diagonal
        homoscedastic noise matrix whose diagonal is the value of the
        `self.models[j].eps` hyperparameter, and :math:`Y(X_{N_i})_{:,j}` is the
        `(batch_count,)` vector of the jth responses of the nearest neighbors
        given by a slice of the `batch_nn_targets` argument.

        If `variance_mode == "diagonal"`, also return the local posterior
        variances of each prediction, corresponding to the diagonal elements of
        a covariance matrix. For each batch element :math:`\\mathbf{x}_i`, we
        compute

        .. math::
            Var(\\widehat{Y}_{NN} (\\mathbf{x}_i \\mid X_{N_i}))_j =
                K^{(j)}_\\theta (\\mathbf{x}_i, \\mathbf{x}_i) -
                K^{(j)}_\\theta (\\mathbf{x}_i, X_{N_i})
                (K^{(j)}_\\theta (X_{N_i}, X_{N_i}) + \\varepsilon I_k)^{-1}
                K^{(j)}_\\theta (X_{N_i}, \\mathbf{x}_i).

        Args:
            pairwise_dists:
                A tensor of shape `(batch_count, nn_count, nn_count)` containing
                the `(nn_count, nn_count)` -shaped pairwise nearest neighbor
                distance matrices corresponding to each of the batch elements.
            crosswise_dists:
                A matrix of shape `(batch_count, nn_count)` whose rows list the
                distance between each batch element element and its nearest
                neighbors.
            batch_nn_targets:
                A tensor of shape `(batch_count, nn_count, response_count)`
                listing the vector-valued responses for the nearest neighbors
                of each batch element.
            variance_mode:
                Specifies the type of variance to return. Currently supports
                `"diagonal"` and None. If None, report no variance term.
            apply_sigma_sq:
                Indicates whether to scale the posterior variance by `sigma_sq`.
                Unused if `variance_mode is None` or
                `sigma_sq.leanred() is False`.


        Returns
        -------
        responses:
            A matrix of shape `(batch_count, response_count)` whose rows are
            the predicted response for each of the given indices.
        diagonal_variance:
            A vector of shape `(batch_count, response_count)` consisting of the
            diagonal elements of the posterior variance for each model. Only
            returned where `variance_mode == "diagonal"`.
        """
        return self._regress(
            self.models,
            pairwise_dists,
            crosswise_dists,
            batch_nn_targets,
            self.sigma_sq,
            variance_mode=variance_mode,
            apply_sigma_sq=(apply_sigma_sq and self.sigma_sq.trained),
        )

    @staticmethod
    def _regress(
        models: List[MuyGPS],
        pairwise_dists: mm.ndarray,
        crosswise_dists: mm.ndarray,
        batch_nn_targets: mm.ndarray,
        sigma_sq: SigmaSq,
        variance_mode: Optional[str] = None,
        apply_sigma_sq: bool = True,
    ) -> Union[mm.ndarray, Tuple[mm.ndarray, mm.ndarray]]:
        batch_count, nn_count, response_count = batch_nn_targets.shape
        responses = mm.zeros((batch_count, response_count))
        if variance_mode is None:
            pass
        elif variance_mode == "diagonal":
            diagonal_variance = mm.zeros((batch_count, response_count))
        else:
            raise NotImplementedError(
                f"Variance mode {variance_mode} is not implemented."
            )
        for i, model in enumerate(models):
            K = model.kernel(pairwise_dists)
            Kcross = model.kernel(crosswise_dists)
            responses = mm.assign(
                responses,
                model._compute_solve(
                    K,
                    Kcross,
                    batch_nn_targets[:, :, i].reshape(batch_count, nn_count, 1),
                    model.eps(),
                ).reshape(batch_count),
                slice(None),
                i,
            )
            if variance_mode == "diagonal":
                ss = sigma_sq()[i] if apply_sigma_sq else 1.0
                diagonal_variance = mm.assign(
                    diagonal_variance,
                    model._compute_diagonal_variance(
                        K, Kcross, model.eps()
                    ).reshape(batch_count)
                    * ss,
                    slice(None),
                    i,
                )
                # if apply_sigma_sq:
                #     diagonal_variance = mm.assign(
                #         diagonal_variance,
                #         diagonal_variance[:, i] * sigma_sq()[i],
                #         slice(None),
                #         i,
                #     )
        if variance_mode == "diagonal":
            return responses, diagonal_variance
        return responses

    def build_fast_regress_coeffs(
        self,
        train: mm.ndarray,
        nn_indices: mm.ndarray,
        targets: mm.ndarray,
        indices_by_rank: bool = False,
    ) -> mm.ndarray:
        """
        Produces coefficient tensor for fast regression given in Equation
        (8) of [dunton2022fast]_. To form the tensor, we compute

        .. math::
            \\mathbf{C}_{N^*}(i, :, j) =
                (K_{\\hat{\\theta_j}} (X_{N^*}, X_{N^*}) +
                \\varepsilon I_k)^{-1} Y(X_{N^*}).

        Here :math:`X_{N^*}` is the union of the nearest neighbor of the ith
        test point and the `nn_count - 1` nearest neighbors of this nearest
        neighbor, :math:`K_{\\hat{\\theta_j}}` is the trained kernel functor
        corresponding the jth response and specified by `self.models`,
        :math:`\\varepsilon I_k` is a diagonal homoscedastic noise matrix whose
        diagonal  is the value of the `self.eps` hyperparameter,
        and :math:`Y(X_{N^*})` is the `(train_count, response_count)`
        matrix of responses corresponding to the training features indexed
        by $N^*$.

        Args:
            train:
                The full training data matrix of shape
                `(train_count, feature_count)`.
            nn_indices:
                The nearest neighbors indices of each
                training points of shape `(train_count, nn_count)`.
            targets:
                A matrix of shape `(train_count, response_count)` whose rows are
                vector-valued responses for each training element.
        Returns:
            A tensor of shape `(batch_count, nn_count, response_count)`
            whose entries comprise the precomputed coefficients for fast
            regression.

        """
        (
            pairwise_dists_fast,
            train_nn_targets_fast,
        ) = _make_fast_regress_tensors(self.metric, nn_indices, train, targets)

        return self._build_fast_regress_coeffs(
            self.models, pairwise_dists_fast, train_nn_targets_fast
        )

    @staticmethod
    def _build_fast_regress_coeffs(
        models: List[MuyGPS],
        pairwise_dists_fast: mm.ndarray,
        train_nn_targets_fast: mm.ndarray,
    ) -> mm.ndarray:
        train_count, nn_count, response_count = train_nn_targets_fast.shape
        coeffs_tensor = mm.zeros((train_count, nn_count, response_count))
        for i, model in enumerate(models):
            K = model.kernel(pairwise_dists_fast)
            mm.assign(
                coeffs_tensor,
                _muygps_fast_regress_precompute(
                    _homoscedastic_perturb(K, model.eps()),
                    train_nn_targets_fast[:, :, i],
                ),
                slice(None),
                slice(None),
                i,
            )

        return coeffs_tensor

    def fast_regress_from_indices(
        self,
        indices: mm.ndarray,
        nn_indices: mm.ndarray,
        test_features: mm.ndarray,
        train_features: mm.ndarray,
        closest_index: mm.ndarray,
        coeffs_tensor: mm.ndarray,
    ) -> mm.ndarray:
        """
        Performs fast multivariate regression using provided
        vectors and matrices used in constructed the crosswise distances matrix,
        the index of the training point closest to the queried test point,
        and precomputed coefficient matrix.

        Returns the predicted response in the form of a posterior
        mean for each element of the batch of observations, as computed in
        Equation (9) of [dunton2022fast]_. For each test point
        :math:`\\mathbf{z}`, we compute

        .. math::
            \\widehat{Y} (\\mathbf{z} \\mid X) =
                K_\\theta (\\mathbf{z}, X_{N^*}) \mathbf{C}_{N^*}.

        Here :math:`X_{N^*}` is the union of the nearest neighbor of the queried
        test point :math:`\\mathbf{z}` and the nearest neighbors of that
        training point, :math:`K_\\theta` is the kernel functor specified
        by `self.kernel`, and :math:`\mathbf{C}_{N^*}` is the matrix of
        precomputed coefficients given in Equation (8) of [dunton2022fast]_.

        Args:
            indices:
                A vector of shape `('batch_count,)` providing the indices of the
                test features to be queried in the formation of the crosswise
                distance tensor.
            nn_indices:
                A matrix of shape `('batch_count, nn_count)` providing the index
                of the closest training point to each queried test point, as
                well as the `nn_count - 1` closest neighbors of that point.
            test_features:
                A matrix of shape `(batch_count, feature_count)` containing
                the test data points.
            train_features:
                A matrix of shape `(train_count, feature_count)` containing the
                training data.
            closest_index:
                A vector of shape `(batch_count,)` for which each entry is
                the index of the training point closest to each queried
                test point.
            coeffs_tensor:
                A tensor of shape `(batch_count, nn_count, response_count)`
                providing the precomputed coefficients for fast regression.

        Returns:
            A matrix of shape `(batch_count, response_count)` whose rows are
            the predicted response for each of the given indices.
        """

        crosswise_dists = crosswise_distances(
            test_features,
            train_features,
            indices,
            nn_indices,
        )

        return self.fast_regress(
            crosswise_dists,
            coeffs_tensor[closest_index, :, :],
        )

    def fast_regress(
        self,
        crosswise_dists: mm.ndarray,
        coeffs_tensor: mm.ndarray,
    ) -> mm.ndarray:
        """
        Performs fast regression using provided
        crosswise distances and precomputed coefficient matrix.

        Returns the predicted response in the form of a posterior
        mean for each element of the batch of observations, as computed in
        Equation (9) of [dunton2022fast]_. For each test point
        :math:`\\mathbf{z}`, we compute

        .. math::
            \\widehat{Y} (\\mathbf{z} \\mid X) =
                K_\\theta (\\mathbf{z}, X_{N^*}) \mathbf{C}_{N^*}.

        Here :math:`X_{N^*}` is the union of the nearest neighbor of the queried
        test point :math:`\\mathbf{z}` and the nearest neighbors of that
        training point, :math:`K_\\theta` is the kernel functor specified by
        `self.kernel`, and :math:`\mathbf{C}_{N^*}` is the matrix of
        precomputed coefficients given in Equation (8) of [dunton2022fast]_.

        Args:
            crosswise_dists:
                A matrix of shape `(batch_count, nn_count)` whose rows list the
                distance of the corresponding test element to each of its
                nearest neighbors.
            coeffs_tensor:
                A tensor of shape `(batch_count, nn_count, response_count)`
                providing the precomputed coefficients for fast regression.


        Returns:
            A matrix of shape `(batch_count, response_count)` whose rows are
            the predicted response for each of the given indices.
        """
        models = self.models
        responses = self._fast_regress(models, crosswise_dists, coeffs_tensor)
        return responses

    @staticmethod
    def _fast_regress(
        models: List[MuyGPS],
        crosswise_dists: mm.ndarray,
        coeffs_tensor: mm.ndarray,
    ) -> mm.ndarray:
        Kcross = mm.zeros(coeffs_tensor.shape)
        for i, model in enumerate(models):
            mm.assign(
                Kcross,
                model.kernel(crosswise_dists),
                slice(None),
                slice(None),
                i,
            )
        return _mmuygps_fast_regress_solve(Kcross, coeffs_tensor)
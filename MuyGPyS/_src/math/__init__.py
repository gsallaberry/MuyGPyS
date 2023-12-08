# Copyright 2021-2023 Lawrence Livermore National Security, LLC and other
# MuyGPyS Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: MIT

from MuyGPyS._src.util import _collect_implementation

(
    all,
    allclose,
    arange,
    argmax,
    array,
    atleast_1d,
    atleast_2d,
    assign,
    corrcoef,
    cov,
    cholesky,
    eye,
    exp,
    diagonal,
    divide,
    iarray,
    inf,
    int32,
    int64,
    itype,
    isclose,
    farray,
    float32,
    float64,
    ftype,
    full,
    linalg,
    linspace,
    log,
    logical_or,
    max,
    median,
    min,
    ndarray,
    ones,
    outer,
    parameter,
    prod,
    repeat,
    reshape,
    sqrt,
    squeeze,
    sum,
    tile,
    unique,
    vstack,
    where,
    zeros,
) = _collect_implementation(
    "MuyGPyS._src.math",
    "all",
    "allclose",
    "arange",
    "argmax",
    "array",
    "atleast_1d",
    "atleast_2d",
    "assign",
    "corrcoef",
    "cov",
    "cholesky",
    "eye",
    "exp",
    "diagonal",
    "divide",
    "iarray",
    "inf",
    "int32",
    "int64",
    "itype",
    "isclose",
    "farray",
    "float32",
    "float64",
    "ftype",
    "full",
    "linalg",
    "linspace",
    "log",
    "logical_or",
    "max",
    "median",
    "min",
    "ndarray",
    "ones",
    "outer",
    "parameter",
    "prod",
    "repeat",
    "reshape",
    "sqrt",
    "squeeze",
    "sum",
    "tile",
    "unique",
    "vstack",
    "where",
    "zeros",
)


def promote(x):
    if isinstance(x, ndarray):
        return x
    else:
        return array(x)

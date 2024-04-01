#!/bin/bash

export LUBAN_SPECFIC_VERSION="2.3.0.18"

source .codebase/patch/_codebase_prepare.sh
if [[ -n "${CUSTOM_PYTHON_VERSION:-}" ]]; then
    python/build-wheel-manylinux2014.sh ${CUSTOM_PYTHON_VERSION}
else
    python/build-wheel-manylinux2014.sh cp37-cp37m,cp38-cp38,cp39-cp39,cp310-cp310
fi

cp -r .whl dist/
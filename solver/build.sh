#!/usr/bin/env bash
# Build TexasSolver's console solver with this repo's patch.
#   solver/build.sh <work dir>
# Leaves <work dir>/TexasSolver/build/console_solver. On Linux the binary is
# linked statically so it runs in claude.ai's sandbox without extra libraries.
set -euo pipefail
WORK=${1:?usage: build.sh <work dir>}
COMMIT=02c8021ee1307c18aa1e76b772c6ae1451601bc6  # upstream console branch
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$WORK" && cd "$WORK"
if [ ! -d TexasSolver ]; then
  git clone --branch console https://github.com/bupticybee/TexasSolver.git
fi
cd TexasSolver
git checkout --quiet "$COMMIT"
git apply "$HERE/texassolver.patch"
FLAGS=(-DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5)
if [ "$(uname)" = Linux ]; then
  FLAGS+=(-DCMAKE_EXE_LINKER_FLAGS="-static -pthread")
else  # macOS: Apple clang needs Homebrew's libomp (brew install cmake libomp)
  OMP=$(brew --prefix libomp)
  FLAGS+=(-DOpenMP_CXX_FLAGS="-Xpreprocessor -fopenmp -I$OMP/include" -DOpenMP_CXX_LIB_NAMES=omp
          -DOpenMP_C_FLAGS="-Xpreprocessor -fopenmp -I$OMP/include" -DOpenMP_C_LIB_NAMES=omp
          -DOpenMP_omp_LIBRARY="$OMP/lib/libomp.dylib"
          -DCMAKE_EXE_LINKER_FLAGS="-L$OMP/lib -lomp" -DCMAKE_SHARED_LINKER_FLAGS="-L$OMP/lib -lomp")
fi
cmake -S . -B build "${FLAGS[@]}"
make -C build -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu)" console_solver

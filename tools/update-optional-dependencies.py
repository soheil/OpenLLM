#!/usr/bin/env python3
# Copyright 2023 BentoML Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations

import os
import shutil

import typing as t
import dataclasses
import inflection
import tomlkit

import openllm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclasses.dataclass(frozen=True)
class Dependencies:
    name: str
    git_repo_url: t.Optional[str] = None
    branch: t.Optional[str] = None
    extensions: t.Optional[t.List[str]] = None

    lower_constraint: t.Optional[str] = None

    def with_options(self, **kwargs: t.Any) -> Dependencies:
        return dataclasses.replace(self, **kwargs)

    @property
    def has_constraint(self) -> bool:
        return self.lower_constraint is not None

    @property
    def pypi_extensions(self) -> str:
        return "" if self.extensions is None else f"[{','.join(self.extensions)}]"

    def to_str(self) -> str:
        if self.lower_constraint is not None:
            return f"{self.name}{self.pypi_extensions}>={self.lower_constraint}"
        elif self.branch is not None:
            return f"{self.name}{self.pypi_extensions} @ git+https://github.com/{self.git_repo_url}.git@{self.branch}"
        else:
            return f"{self.name}{self.pypi_extensions}"

    @classmethod
    def from_tuple(cls, *decls: t.Any) -> Dependencies:
        return cls(*decls)


_BENTOML_EXT = ["grpc", "io"]
_TRANSFORMERS_EXT = ["torch", "tokenizers", "accelerate"]

_BASE_DEPENDENCIES = [
    Dependencies(name="bentoml", extensions=_BENTOML_EXT, lower_constraint="1.0.22"),
    Dependencies(name="transformers", extensions=_TRANSFORMERS_EXT, lower_constraint="4.29.0"),
    Dependencies(name="optimum"),
    Dependencies(name="attrs", lower_constraint="23.1.0"),
    Dependencies(name="cattrs", lower_constraint="23.1.0"),
    Dependencies(name="orjson"),
    Dependencies(name="inflection"),
    Dependencies(name="tabulate", extensions=["widechars"], lower_constraint="0.9.0"),
    Dependencies(name="httpx"),
    Dependencies(name="typing_extensions"),
]

_NIGHTLY_MAPPING: dict[str, Dependencies] = {
    "bentoml": Dependencies.from_tuple("bentoml", "bentoml/bentoml", "main", _BENTOML_EXT),
    "peft": Dependencies.from_tuple("peft", "huggingface/peft", "main", None),
    "transformers": Dependencies.from_tuple("transformers", "huggingface/transformers", "main", _TRANSFORMERS_EXT),
    "optimum": Dependencies.from_tuple("optimum", "huggingface/optimum", "main", None),
    "accelerate": Dependencies.from_tuple("accelerate", "huggingface/accelerate", "main", None),
    "bitsandbytes": Dependencies.from_tuple("bitsandbytes", "TimDettmers/bitsandbytes", "main", None),
}

NIGHTLY_DEPS = [v.to_str() for v in _NIGHTLY_MAPPING.values()]
FINE_TUNE_DEPS = ["peft", "bitsandbytes", "datasets", "accelerate", "deepspeed"]
FLAN_T5_DEPS = ["flax", "jax", "jaxlib", "tensorflow", "keras"]
OPENAI_DEPS = ["openai", "tiktoken"]
AGENTS_DEPS = ["transformers[agents]", "diffusers", "soundfile"]

_base_requirements = {
    inflection.dasherize(name): config_cls.__openllm_requirements__
    for name, config_cls in openllm.CONFIG_MAPPING.items()
    if config_cls.__openllm_requirements__
}

# shallow copy from locals()
_locals = locals().copy()

# NOTE: update this table when adding new external dependencies
# sync with openllm.utils.OPTIONAL_DEPENDENCIES
_base_requirements.update(
    {v: _locals[f"{inflection.underscore(v).upper()}_DEPS"] for v in openllm.utils.OPTIONAL_DEPENDENCIES}
)


def main() -> int:
    with open(os.path.join(ROOT, "pyproject.toml"), "r") as f:
        pyproject = tomlkit.parse(f.read())

    table = tomlkit.table()
    for name, config in _base_requirements.items():
        table.add(name, config)

    # ignore nightly for all
    table.add("all", [f"openllm[{k}]" for k in table.keys() if k != "nightly"])

    pyproject["project"]["optional-dependencies"] = table

    # write project dependencies
    pyproject["project"]["dependencies"] = [v.to_str() for v in _BASE_DEPENDENCIES]
    with open(os.path.join(ROOT, "pyproject.toml"), "w") as f:
        f.write(tomlkit.dumps(pyproject))

    if shutil.which("taplo"):
        return os.system(f"taplo fmt {os.path.join(ROOT, 'pyproject.toml')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

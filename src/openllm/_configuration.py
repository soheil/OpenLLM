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
"""
Configuration utilities for OpenLLM. All model configuration will inherit from ``openllm.LLMConfig``.

Highlight feature: Each fields in ``openllm.LLMConfig`` will also automatically generate a environment
variable based on its name field.

For example, the following config class:

```python
class FlanT5Config(openllm.LLMConfig):
    __config__ = {
        "url": "https://huggingface.co/docs/transformers/model_doc/flan-t5",
        "default_id": "google/flan-t5-large",
        "model_ids": [
            "google/flan-t5-small",
            "google/flan-t5-base",
            "google/flan-t5-large",
            "google/flan-t5-xl",
            "google/flan-t5-xxl",
        ],
    }

    class GenerationConfig:
        temperature: float = 0.9
        max_new_tokens: int = 2048
        top_k: int = 50
        top_p: float = 0.4
        repetition_penalty = 1.0
```

which generates the environment OPENLLM_FLAN_T5_GENERATION_TEMPERATURE for users to configure temperature
dynamically during serve, ahead-of-serve or per requests.

Refer to ``openllm.LLMConfig`` docstring for more information.
"""
from __future__ import annotations

import functools
import inspect
import logging
import os
import sys
import typing as t
from operator import itemgetter

import attr
import click_option_group as cog
import inflection
import orjson
from cattr.gen import make_dict_unstructure_fn
from cattr.gen import override
from deepmerge.merger import Merger

import openllm

from .exceptions import ForbiddenAttributeError
from .utils import DEBUG
from .utils import ENV_VARS_TRUE_VALUES
from .utils import LazyType
from .utils import bentoml_cattr
from .utils import codegen
from .utils import dantic
from .utils import first_not_none
from .utils import lenient_issubclass
from .utils import non_intrusive_setattr


if hasattr(t, "Required"):
    from typing import Required
else:
    from typing_extensions import Required

if hasattr(t, "NotRequired"):
    from typing import NotRequired
else:
    from typing_extensions import NotRequired

if hasattr(t, "dataclass_transform"):
    from typing import dataclass_transform
else:
    from typing_extensions import dataclass_transform

_T = t.TypeVar("_T")


if t.TYPE_CHECKING:
    from attr import _CountingAttr  # type: ignore
    from attr import _make_init  # type: ignore
    from attr import _make_repr  # type: ignore
    from attr import _transform_attrs  # type: ignore

    import transformers
    from transformers.generation.beam_constraints import Constraint

    from ._types import ClickFunctionWrapper
    from ._types import F
    from ._types import O_co
    from ._types import P

    ReprArgs: t.TypeAlias = t.Iterable[tuple[str | None, t.Any]]

    DictStrAny = dict[str, t.Any]
    ListStr = list[str]
    ItemgetterAny = itemgetter[t.Any]
    FieldTransformers = t.Callable[[_T, list[attr.Attribute[t.Any]]], list[attr.Attribute[t.Any]]]
else:
    Constraint = t.Any
    ListStr = list
    DictStrAny = dict
    ItemgetterAny = itemgetter
    # NOTE: Using internal API from attr here, since we are actually
    # allowing subclass of openllm.LLMConfig to become 'attrs'-ish
    from attr._make import _CountingAttr
    from attr._make import _make_init
    from attr._make import _make_repr
    from attr._make import _transform_attrs

    transformers = openllm.utils.LazyLoader("transformers", globals(), "transformers")

__all__ = ["LLMConfig"]

logger = logging.getLogger(__name__)

config_merger = Merger(
    # merge dicts
    type_strategies=[(DictStrAny, "merge")],
    # override all other types
    fallback_strategies=["override"],
    # override conflicting types
    type_conflict_strategies=["override"],
)


@attr.frozen(slots=True)
class GenerationConfig:
    """Generation config provides the configuration to then be parsed to ``transformers.GenerationConfig``,
    with some additional validation and environment constructor.

    Note that we always set `do_sample=True`. This class is not designed to be used directly, rather
    to be used conjunction with LLMConfig. The instance of the generation config can then be accessed
    via ``LLMConfig.generation_config``.
    """

    # NOTE: parameters for controlling the length of the output
    max_new_tokens: int = dantic.Field(
        20,
        ge=0,
        description="The maximum numbers of tokens to generate, ignoring the number of tokens in the prompt.",
    )
    min_length: int = dantic.Field(
        0,
        ge=0,
        description="""The minimum length of the sequence to be generated. Corresponds to the length of the
        input prompt + `min_new_tokens`. Its effect is overridden by `min_new_tokens`, if also set.""",
    )
    min_new_tokens: int = dantic.Field(
        description="The minimum numbers of tokens to generate, ignoring the number of tokens in the prompt.",
    )
    early_stopping: bool = dantic.Field(
        False,
        description="""Controls the stopping condition for beam-based methods, like beam-search. It accepts the
        following values: `True`, where the generation stops as soon as there are `num_beams` complete candidates;
        `False`, where an heuristic is applied and the generation stops when is it very unlikely to find
        better candidates; `"never"`, where the beam search procedure only stops when there
        cannot be better candidates (canonical beam search algorithm)
    """,
    )
    max_time: float = dantic.Field(
        description="""The maximum amount of time you allow the computation to run for in seconds. generation will
        still finish the current pass after allocated time has been passed.""",
    )

    # NOTE: Parameters for controling generaiton strategies
    num_beams: int = dantic.Field(1, description="Number of beams for beam search. 1 means no beam search.")
    num_beam_groups: int = dantic.Field(
        1,
        description="""Number of groups to divide `num_beams` into in order to ensure diversity among different
        groups of beams. [this paper](https://arxiv.org/pdf/1610.02424.pdf) for more details.""",
    )
    penalty_alpha: float = dantic.Field(
        description="""The values balance the model confidence and the degeneration penalty in
        contrastive search decoding.""",
    )
    use_cache: bool = dantic.Field(
        True,
        description="""Whether or not the model should use the past last
        key/values attentions (if applicable to the model) to speed up decoding.""",
    )

    # NOTE: Parameters for manipulation of the model output logits
    temperature: float = dantic.Field(
        1.0, ge=0.0, le=1.0, description="The value used to modulate the next token probabilities."
    )
    top_k: int = dantic.Field(
        50, description="The number of highest probability vocabulary tokens to keep for top-k-filtering."
    )
    top_p: float = dantic.Field(
        1.0,
        description="""If set to float < 1, only the smallest set of most probable tokens with
        probabilities that add up to `top_p` or higher are kept for generation.""",
    )
    typical_p: float = dantic.Field(
        1.0,
        description="""Local typicality measures how similar the conditional probability of predicting a target
        token next is to the expected conditional probability of predicting a random token next, given the
        partial text already generated. If set to float < 1, the smallest set of the most locally typical
        tokens with probabilities that add up to `typical_p` or higher are kept for generation. See [this
        paper](https://arxiv.org/pdf/2202.00666.pdf) for more details.
    """,
    )
    epsilon_cutoff: float = dantic.Field(
        0.0,
        description="""\
        If set to float strictly between 0 and 1, only tokens with a conditional probability greater than
        `epsilon_cutoff` will be sampled. In the paper, suggested values range from 3e-4 to 9e-4, depending on the
        size of the model. See [Truncation Sampling as Language Model Desmoothing](https://arxiv.org/abs/2210.15191)
        for more details.
    """,
    )
    eta_cutoff: float = dantic.Field(
        0.0,
        description="""Eta sampling is a hybrid of locally typical sampling and epsilon sampling.
        If set to float strictly between 0 and 1, a token is only considered if it is greater than
        either `eta_cutoff` or `sqrt(eta_cutoff) * exp(-entropy(softmax(next_token_logits)))`. The latter term is
        intuitively the expected next token probability, scaled by `sqrt(eta_cutoff)`. In the paper, suggested
        values range from 3e-4 to 2e-3, depending on the size of the model.
        See [Truncation Sampling as Language Model Desmoothing](https://arxiv.org/abs/2210.15191) for more details.
    """,
    )
    diversity_penalty: float = dantic.Field(
        0.0,
        description="""This value is subtracted from a beam's score if it generates a token same
        as any beam from other group at a particular time. Note that `diversity_penalty` is only
        effective if `group beam search` is enabled.
    """,
    )
    repetition_penalty: float = dantic.Field(
        1.0,
        description="""The parameter for repetition penalty. 1.0 means no penalty.
        See [this paper](https://arxiv.org/pdf/1909.05858.pdf) for more details.""",
    )
    encoder_repetition_penalty: float = dantic.Field(
        1.0,
        description="""The paramater for encoder_repetition_penalty. An exponential penalty on sequences that are
        not in the original input. 1.0 means no penalty.""",
    )
    length_penalty: float = dantic.Field(
        1.0,
        description="""Exponential penalty to the length that is used with beam-based generation. It is applied
        as an exponent to the sequence length, which in turn is used to divide the score of the sequence. Since
        the score is the log likelihood of the sequence (i.e. negative), `length_penalty` > 0.0 promotes longer
        sequences, while `length_penalty` < 0.0 encourages shorter sequences.
    """,
    )
    no_repeat_ngram_size: int = dantic.Field(
        0, description="If set to int > 0, all ngrams of that size can only occur once."
    )
    bad_words_ids: t.List[t.List[int]] = dantic.Field(
        description="""List of token ids that are not allowed to be generated. In order to get the token ids
        of the words that should not appear in the generated text, use
        `tokenizer(bad_words, add_prefix_space=True, add_special_tokens=False).input_ids`.
        """,
    )

    # NOTE: t.Union is not yet supported on CLI, but the environment variable should already be available.
    force_words_ids: t.Union[t.List[t.List[int]], t.List[t.List[t.List[int]]]] = dantic.Field(
        description="""List of token ids that must be generated. If given a `List[List[int]]`, this is treated
        as a simple list of words that must be included, the opposite to `bad_words_ids`.
        If given `List[List[List[int]]]`, this triggers a
        [disjunctive constraint](https://github.com/huggingface/transformers/issues/14081), where one
        can allow different forms of each word.
        """,
    )
    renormalize_logits: bool = dantic.Field(
        False,
        description="""Whether to renormalize the logits after applying all the logits processors or warpers
        (including the custom ones). It's highly recommended to set this flag to `True` as the search
        algorithms suppose the score logits are normalized but some logit processors or warpers break the normalization.
    """,
    )
    constraints: t.List[Constraint] = dantic.Field(
        description="""Custom constraints that can be added to the generation to ensure that the output
        will contain the use of certain tokens as defined by ``Constraint`` objects, in the most sensible way possible.
        """,
    )
    forced_bos_token_id: int = dantic.Field(
        description="""The id of the token to force as the first generated token after the
        ``decoder_start_token_id``. Useful for multilingual models like
        [mBART](https://huggingface.co/docs/transformers/model_doc/mbart) where the first generated token needs
        to be the target language token.
    """,
    )
    forced_eos_token_id: t.Union[int, t.List[int]] = dantic.Field(
        description="""The id of the token to force as the last generated token when `max_length` is reached.
        Optionally, use a list to set multiple *end-of-sequence* tokens.""",
    )
    remove_invalid_values: bool = dantic.Field(
        False,
        description="""Whether to remove possible *nan* and *inf* outputs of the model to prevent the
        generation method to crash. Note that using `remove_invalid_values` can slow down generation.""",
    )
    exponential_decay_length_penalty: t.Tuple[int, float] = dantic.Field(
        description="""This tuple adds an exponentially increasing length penalty, after a certain amount of tokens
        have been generated. The tuple shall consist of: `(start_index, decay_factor)` where `start_index`
        indicates where penalty starts and `decay_factor` represents the factor of exponential decay
    """,
    )
    suppress_tokens: t.List[int] = dantic.Field(
        description="""A list of tokens that will be suppressed at generation. The `SupressTokens` logit
        processor will set their log probs to `-inf` so that they are not sampled.
    """,
    )
    begin_suppress_tokens: t.List[int] = dantic.Field(
        description="""A list of tokens that will be suppressed at the beginning of the generation. The
        `SupressBeginTokens` logit processor will set their log probs to `-inf` so that they are not sampled.
        """,
    )
    forced_decoder_ids: t.List[t.List[int]] = dantic.Field(
        description="""A list of pairs of integers which indicates a mapping from generation indices to token indices
        that will be forced before sampling. For example, `[[1, 123]]` means the second generated token will always
        be a token of index 123.
        """,
    )

    # NOTE: Parameters that define the output variables of `generate`
    num_return_sequences: int = dantic.Field(
        1, description="The number of independently computed returned sequences for each element in the batch."
    )
    output_attentions: bool = dantic.Field(
        False,
        description="""Whether or not to return the attentions tensors of all attention layers.
        See `attentions` under returned tensors for more details. """,
    )
    output_hidden_states: bool = dantic.Field(
        False,
        description="""Whether or not to return the hidden states of all layers.
        See `hidden_states` under returned tensors for more details.
        """,
    )
    output_scores: bool = dantic.Field(
        False,
        description="""Whether or not to return the prediction scores. See `scores` under returned
        tensors for more details.""",
    )

    # NOTE: Special tokens that can be used at generation time
    pad_token_id: int = dantic.Field(description="The id of the *padding* token.")
    bos_token_id: int = dantic.Field(description="The id of the *beginning-of-sequence* token.")
    eos_token_id: t.Union[int, t.List[int]] = dantic.Field(
        description="""The id of the *end-of-sequence* token. Optionally, use a list to set
        multiple *end-of-sequence* tokens.""",
    )

    # NOTE: Generation parameters exclusive to encoder-decoder models
    encoder_no_repeat_ngram_size: int = dantic.Field(
        0,
        description="""If set to int > 0, all ngrams of that size that occur in the
        `encoder_input_ids` cannot occur in the `decoder_input_ids`.
        """,
    )
    decoder_start_token_id: int = dantic.Field(
        description="""If an encoder-decoder model starts decoding with a
        different token than *bos*, the id of that token.
        """,
    )

    if t.TYPE_CHECKING:

        def __attrs_init__(self, **_: t.Any):
            ...

    def __init__(self, *, _internal: bool = False, **attrs: t.Any):
        if not _internal:
            raise RuntimeError(
                "GenerationConfig is not meant to be used directly, "
                "but you can access this via a LLMConfig.generation_config"
            )
        self.__attrs_init__(**attrs)

    def __getitem__(self, item: str) -> t.Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(f"GenerationConfig has no attribute {item}")


bentoml_cattr.register_unstructure_hook_factory(
    lambda cls: attr.has(cls) and lenient_issubclass(cls, GenerationConfig),
    lambda cls: make_dict_unstructure_fn(
        cls,
        bentoml_cattr,
        **{k: override(omit=True) for k, v in attr.fields_dict(cls).items() if v.default in (None, attr.NOTHING)},
    ),
)


def _field_env_key(model_name: str, key: str, suffix: str | t.Literal[""] | None = None) -> str:
    return "_".join(filter(None, map(str.upper, ["OPENLLM", model_name, suffix.strip("_") if suffix else "", key])))


# cached it here to save one lookup per assignment
_object_getattribute = object.__getattribute__


class ModelSettings(t.TypedDict, total=False):
    """ModelSettings serve only for typing purposes as this is transcribed into LLMConfig.__config__.
    Note that all fields from this dictionary will then be converted to __openllm_*__ fields in LLMConfig.
    """

    # NOTE: These required fields should be at the top, as it will be kw_only
    default_id: Required[str]
    model_ids: Required[ListStr]

    # meta
    url: str
    requires_gpu: bool
    trust_remote_code: bool
    service_name: NotRequired[str]
    requirements: t.Optional[ListStr]

    # llm implementation specifics
    use_pipeline: bool
    bettertransformer: bool
    model_type: t.Literal["causal_lm", "seq2seq_lm"]
    runtime: t.Literal["transformers", "cpp"]

    # naming convention, only name_type is needed to infer from the class
    # as the three below it can be determined automatically
    name_type: t.Literal["dasherize", "lowercase"]
    model_name: NotRequired[str]
    start_name: NotRequired[str]
    env: NotRequired[openllm.utils.ModelEnv]

    # serving configuration
    timeout: int
    workers_per_resource: t.Union[int, float]

    # the target generation_config class to be used.
    generation_class: t.Type[GenerationConfig]


def _settings_field_transformer(
    _: type[attr.AttrsInstance], __: list[attr.Attribute[t.Any]]
) -> list[attr.Attribute[t.Any]]:
    return [
        attr.Attribute.from_counting_attr(
            k,
            dantic.Field(
                kw_only=False if t.get_origin(ann) is not Required else True,
                auto_default=True,
                use_default_converter=False,
                type=ann,
                metadata={"target": f"__openllm_{k}__"},
                description=f"ModelSettings field for {k}.",
            ),
        )
        for k, ann in t.get_type_hints(ModelSettings).items()
    ]


@attr.define(slots=True, field_transformer=_settings_field_transformer, frozen=False)
class _ModelSettingsAttr:
    """Internal attrs representation of ModelSettings."""

    def __getitem__(self, key: str) -> t.Any:
        if key in codegen.get_annotations(ModelSettings):
            return _object_getattribute(self, key)
        raise KeyError(key)

    @classmethod
    def default(cls) -> _ModelSettingsAttr:
        _ = ModelSettings(
            default_id="__default__",
            model_ids=["__default__"],
            name_type="dasherize",
            requires_gpu=False,
            url="",
            use_pipeline=False,
            model_type="causal_lm",
            trust_remote_code=False,
            requirements=None,
            timeout=3600,
            service_name="",
            workers_per_resource=1,
            runtime="transformers",
        )
        return cls(**t.cast(DictStrAny, _))


def structure_settings(cl_: type[LLMConfig], cls: type[_ModelSettingsAttr]):
    if not lenient_issubclass(cl_, LLMConfig):
        raise RuntimeError(f"Given '{cl_}' must be a subclass type of 'LLMConfig', got '{cl_}' instead.")

    if not hasattr(cl_, "__config__") or getattr(cl_, "__config__") is None:
        raise RuntimeError("Given LLMConfig must have '__config__' that is not None defined.")

    assert cl_.__config__ is not None

    if "generation_class" in cl_.__config__:
        raise ValueError(
            "'generation_class' shouldn't be defined in '__config__', rather defining "
            f"all required attributes under '{cl_}.GenerationConfig' instead."
        )

    _cl_name = cl_.__name__.replace("Config", "")

    _settings_attr = _ModelSettingsAttr.default()
    try:
        cls(**t.cast(DictStrAny, cl_.__config__))
        _settings_attr = attr.evolve(_settings_attr, **t.cast(DictStrAny, cl_.__config__))
    except TypeError:
        raise ValueError("Either 'default_id' or 'model_ids' are emptied under '__config__' (required fields).")

    _final_value_dct: DictStrAny = {
        "model_name": inflection.underscore(_cl_name)
        if _settings_attr["name_type"] == "dasherize"
        else _cl_name.lower()
    }
    _final_value_dct["start_name"] = (
        inflection.dasherize(_final_value_dct["model_name"])
        if _settings_attr["name_type"] == "dasherize"
        else _final_value_dct["model_name"]
    )
    env = openllm.utils.ModelEnv(_final_value_dct["model_name"])
    _final_value_dct["env"] = env

    # bettertransformer support
    if _settings_attr["bettertransformer"] is None:
        _final_value_dct["bettertransformer"] = (
            os.environ.get(env.bettertransformer, str(False)).upper() in ENV_VARS_TRUE_VALUES
        )
    if _settings_attr["requires_gpu"]:
        # if requires_gpu is True, then disable BetterTransformer for quantization.
        _final_value_dct["bettertransformer"] = False

    _final_value_dct["service_name"] = f"generated_{_final_value_dct['model_name']}_service.py"
    _final_value_dct["generation_class"] = attr.make_class(
        f"{_cl_name}GenerationConfig",
        [],
        bases=(GenerationConfig,),
        slots=True,
        weakref_slot=True,
        frozen=True,
        repr=True,
        collect_by_mro=True,
        field_transformer=_make_env_transformer(
            cl_,
            _final_value_dct["model_name"],
            suffix="generation",
            default_callback=lambda field_name, field_default: getattr(cl_.GenerationConfig, field_name, field_default)
            if codegen.has_own_attribute(cl_, "GenerationConfig")
            else field_default,
            globs={"cl_": cl_},
        ),
    )

    return attr.evolve(_settings_attr, **_final_value_dct)


bentoml_cattr.register_structure_hook(_ModelSettingsAttr, structure_settings)


def _make_env_transformer(
    cls: type[LLMConfig],
    model_name: str,
    suffix: t.LiteralString | None = None,
    default_callback: t.Callable[[str, t.Any], t.Any] | None = None,
    globs: DictStrAny | None = None,
):
    def identity(_: str, x_value: t.Any) -> t.Any:
        return x_value

    default_callback = identity if default_callback is None else default_callback

    globs = {} if globs is None else globs
    globs.update(
        {
            "functools": functools,
            "__populate_env": dantic.env_converter,
            "__default_callback": default_callback,
            "__field_env": _field_env_key,
            "__suffix": suffix or "",
            "__model_name": model_name,
        }
    )

    lines: ListStr = [
        "__env = lambda field_name: __field_env(__model_name, field_name, __suffix)",
        "return [",
        "    f.evolve(",
        "        default=__populate_env(__default_callback(f.name, f.default), __env(f.name)),",
        "        metadata={",
        "            'env': f.metadata.get('env', __env(f.name)),",
        "            'description': f.metadata.get('description', '(not provided)'),",
        "        },",
        "    )",
        "    for f in fields",
        "]",
    ]
    fields_ann = "list[attr.Attribute[t.Any]]"

    return codegen.generate_function(
        cls,
        "__auto_env",
        lines,
        args=("_", "fields"),
        globs=globs,
        annotations={"_": "type[LLMConfig]", "fields": fields_ann, "return": fields_ann},
    )


def _setattr_class(attr_name: str, value_var: t.Any, add_dunder: bool = False):
    """
    Use the builtin setattr to set *attr_name* to *value_var*.
    We can't use the cached object.__setattr__ since we are setting
    attributes to a class.

    If add_dunder to True, the generated globs should include a __add_dunder
    value that will be used to add the dunder methods to the class for given
    value_var
    """
    val = f"__add_dunder(cls, {value_var})" if add_dunder else value_var
    return f"setattr(cls, '{attr_name}', {val})"


_dunder_add = {"generation_class"}


def _make_assignment_script(
    cls: type[LLMConfig], attributes: attr.AttrsInstance, _prefix: t.LiteralString = "openllm"
) -> t.Callable[..., None]:
    """Generate the assignment script with prefix attributes __openllm_<value>__"""
    args: ListStr = []
    globs: DictStrAny = {
        "cls": cls,
        "_cached_attribute": attributes,
        "_cached_getattribute_get": _object_getattribute.__get__,
        "__add_dunder": codegen.add_method_dunders,
    }
    annotations: DictStrAny = {"return": None}

    lines: ListStr = ["_getattr = _cached_getattribute_get(_cached_attribute)"]
    for attr_name, field in attr.fields_dict(attributes.__class__).items():
        arg_name = field.metadata.get("target", f"__{_prefix}_{inflection.underscore(attr_name)}__")
        args.append(f"{attr_name}=getattr(_cached_attribute, '{attr_name}')")
        lines.append(_setattr_class(arg_name, attr_name, add_dunder=attr_name in _dunder_add))
        annotations[attr_name] = field.type

    return codegen.generate_function(
        cls, "__assign_attr", lines, args=("cls", *args), globs=globs, annotations=annotations
    )


_reserved_namespace = {"__config__", "GenerationConfig"}


@dataclass_transform(order_default=True, field_specifiers=(attr.field, dantic.Field))
def __llm_config_transform__(cls: type[LLMConfig]) -> type[LLMConfig]:
    kwargs: dict[str, t.Any] = {}
    if hasattr(cls, "GenerationConfig"):
        kwargs = {k: v for k, v in vars(cls.GenerationConfig).items() if not k.startswith("_")}
    non_intrusive_setattr(
        cls,
        "__dataclass_transform__",
        {
            "order_default": True,
            "field_specifiers": (attr.field, dantic.Field),
            "kwargs": kwargs,
        },
    )
    return cls


@attr.define(slots=True)
class LLMConfig:
    """
    ``openllm.LLMConfig`` is somewhat a hybrid combination between the performance of `attrs` with the
    easy-to-use interface that pydantic offer. It lives in between where it allows users to quickly formulate
    a LLMConfig for any LLM without worrying too much about performance. It does a few things:

    - Automatic environment conversion: Each fields will automatically be provisioned with an environment
        variable, make it easy to work with ahead-of-time or during serving time
    - Familiar API: It is compatible with cattrs as well as providing a few Pydantic-2 like API,
        i.e: ``model_construct_env``, ``to_generation_config``, ``to_click_options``
    - Automatic CLI generation: It can identify each fields and convert it to compatible Click options.
        This means developers can use any of the LLMConfig to create CLI with compatible-Python
        CLI library (click, typer, ...)

    > Internally, LLMConfig is an attrs class. All subclass of LLMConfig contains "attrs-like" features,
    > which means LLMConfig will actually generate subclass to have attrs-compatible API, so that the subclass
    > can be written as any normal Python class.

    To directly configure GenerationConfig for any given LLM, create a GenerationConfig under the subclass:

    ```python
    class FlanT5Config(openllm.LLMConfig):
        class GenerationConfig:
            temperature: float = 0.75
            max_new_tokens: int = 3000
            top_k: int = 50
            top_p: float = 0.4
            repetition_penalty = 1.0
    ```
    By doing so, openllm.LLMConfig will create a compatible GenerationConfig attrs class that can be converted
    to ``transformers.GenerationConfig``. These attribute can be accessed via ``LLMConfig.generation_config``.

    By default, all LLMConfig must provide a __config__ with 'default_id' and 'model_ids'.

    All other fields are optional, and will be use default value if not set.

    ```python
    class FalconConfig(openllm.LLMConfig):
        __config__ = {
            "name_type": "lowercase",
            "trust_remote_code": True,
            "requires_gpu": True,
            "timeout": 3600000,
            "url": "https://falconllm.tii.ae/",
            "requirements": ["einops", "xformers", "safetensors"],
            # NOTE: The below are always required
            "default_id": "tiiuae/falcon-7b",
            "model_ids": [
                "tiiuae/falcon-7b",
                "tiiuae/falcon-40b",
                "tiiuae/falcon-7b-instruct",
                "tiiuae/falcon-40b-instruct",
            ],
        }
    ```
    """

    Field = dantic.Field
    """Field is a alias to the internal dantic utilities to easily create
    attrs.fields with pydantic-compatible interface. For example:

    ```python
    class MyModelConfig(openllm.LLMConfig):

        field1 = openllm.LLMConfig.Field(...)
    ```
    """

    # NOTE: The following is handled via __init_subclass__, and is only used for TYPE_CHECKING
    if t.TYPE_CHECKING:
        # NOTE: public attributes to override
        __config__: ModelSettings | None = Field(None)
        """Internal configuration for this LLM model. Each of the field in here will be populated
        and prefixed with __openllm_<value>__"""

        GenerationConfig: type = Field(None)
        """Users can override this subclass of any given LLMConfig to provide GenerationConfig
        default value. For example:

        ```python
        class MyAwesomeModelConfig(openllm.LLMConfig):
            class GenerationConfig:
                max_new_tokens: int = 200
                top_k: int = 10
                num_return_sequences: int = 1
                eos_token_id: int = 11
        ```
        """

        # NOTE: Internal attributes that should only be used by OpenLLM. Users usually shouldn't
        # concern any of these. These are here for pyright not to complain.
        def __attrs_init__(self, **attrs: t.Any):
            """Generated __attrs_init__ for LLMConfig subclass that follows the attrs contract."""

        __attrs_attrs__: tuple[attr.Attribute[t.Any], ...] = Field(None, init=False)
        """Since we are writing our own __init_subclass__, which is an alternative way for __prepare__,
        we want openllm.LLMConfig to be attrs-like dataclass that has pydantic-like interface.
        __attrs_attrs__ will be handled dynamically by __init_subclass__.
        """

        __openllm_hints__: DictStrAny = Field(None, init=False)
        """An internal cache of resolved types for this LLMConfig."""

        __openllm_accepted_keys__: set[str] = Field(None, init=False)
        """The accepted keys for this LLMConfig."""

        __openllm_extras__: DictStrAny = Field(None, init=False)
        """Extra metadata for this LLMConfig."""

        # NOTE: The following will be populated from __config__ and also
        # considered to be public API.
        __openllm_default_id__: str = Field(None)
        """Return the default model to use when using 'openllm start <model_id>'.
        This could be one of the keys in 'self.model_ids' or custom users model.

        This field is required when defining under '__config__'.
        """

        __openllm_model_ids__: ListStr = Field(None)
        """A list of supported pretrained models tag for this given runnable.

        For example:
            For FLAN-T5 impl, this would be ["google/flan-t5-small", "google/flan-t5-base",
                                                "google/flan-t5-large", "google/flan-t5-xl", "google/flan-t5-xxl"]

        This field is required when defining under '__config__'.
        """

        __openllm_url__: str = Field(None, init=False)
        """The resolved url for this LLMConfig."""

        __openllm_requires_gpu__: bool = Field(None, init=False)
        """Determines if this model is only available on GPU. By default it supports GPU and fallback to CPU."""

        __openllm_trust_remote_code__: bool = Field(False)
        """Whether to always trust remote code"""

        __openllm_service_name__: str = Field(None)
        """Generated service name for this LLMConfig. By default, it is 'generated_{model_name}_service.py'"""

        __openllm_requirements__: ListStr | None = Field(None)
        """The default PyPI requirements needed to run this given LLM. By default, we will depend on
        bentoml, torch, transformers."""

        __openllm_use_pipeline__: bool = Field(False)
        """Whether this LLM will use HuggingFace Pipeline API. By default, this is set to False.
        The reason for this to be here is because we want to access this object before loading
        the _bentomodel. This is because we will actually download the model weights when accessing
        _bentomodel.
        """

        __openllm_bettertransformer__: bool = Field(False)
        """Whether to use BetterTransformer for this given LLM. This depends per model
        architecture. By default, we will use BetterTransformer for T5 and StableLM models,
        and set to False for every other models.
        """

        __openllm_model_type__: t.Literal["causal_lm", "seq2seq_lm"] = Field("causal_lm")
        """The model type for this given LLM. By default, it should be causal language modeling.
        Currently supported 'causal_lm' or 'seq2seq_lm'
        """

        __openllm_runtime__: t.Literal["transformers", "cpp"] = Field("transformers")
        """The runtime to use for this model. Possible values are `transformers` or `cpp`. See
        LlaMA for more information."""

        __openllm_name_type__: t.Literal["dasherize", "lowercase"] = Field("dasherize")
        """the default name typed for this model. "dasherize" will convert the name to lowercase and
        replace spaces with dashes. "lowercase" will convert the name to lowercase."""

        __openllm_model_name__: str = Field(None)
        """The normalized version of __openllm_start_name__, determined by __openllm_name_type__"""

        __openllm_start_name__: str = Field(None)
        """Default name to be used with `openllm start`"""

        __openllm_env__: openllm.utils.ModelEnv = Field(None, init=False)
        """A ModelEnv instance for this LLMConfig."""

        __openllm_timeout__: int = Field(36000)
        """The default timeout to be set for this given LLM."""

        __openllm_workers_per_resource__: int | float = Field(1)
        """The number of workers per resource. This is used to determine the number of workers to use for this model.
        For example, if this is set to 0.5, then OpenLLM will use 1 worker per 2 resources. If this is set to 1, then
        OpenLLM will use 1 worker per resource. If this is set to 2, then OpenLLM will use 2 workers per resource.

        See StarCoder for more advanced usage. See
        https://docs.bentoml.org/en/latest/guides/scheduling.html#resource-scheduling-strategy for more details.

        By default, it is set to 1.
        """

        __openllm_generation_class__: type[GenerationConfig] = Field(None, init=False)
        """The result generated GenerationConfig class for this LLMConfig. This will be used
        to create the generation_config argument that can be used throughout the lifecycle.
        This class will also be managed internally by OpenLLM."""

    def __init_subclass__(cls):
        """The purpose of this __init_subclass__ is that we want all subclass of LLMConfig
        to adhere to the attrs contract, and have pydantic-like interface. This means we will
        construct all fields and metadata and hack into how attrs use some of the 'magic' construction
        to generate the fields.

        It also does a few more extra features: It also generate all __openllm_*__ config from
        ModelSettings (derived from __config__) to the class.
        """
        if not cls.__name__.endswith("Config"):
            logger.warning("LLMConfig subclass should end with 'Config'. Updating to %sConfig", cls.__name__)
            cls.__name__ = f"{cls.__name__}Config"

        # NOTE: auto assignment attributes generated from __config__
        _make_assignment_script(cls, bentoml_cattr.structure(cls, _ModelSettingsAttr))(cls)
        # process a fields under cls.__dict__ and auto convert them with dantic.Field
        cd = cls.__dict__
        anns = codegen.get_annotations(cls)

        # _CountingAttr is the underlying representation of attr.field
        ca_names = {name for name, attr in cd.items() if isinstance(attr, _CountingAttr)}
        these: dict[str, _CountingAttr[t.Any]] = {}
        annotated_names: set[str] = set()
        for attr_name, typ in anns.items():
            if codegen.is_class_var(typ):
                continue
            annotated_names.add(attr_name)
            val = cd.get(attr_name, attr.NOTHING)
            if not LazyType["_CountingAttr[t.Any]"](_CountingAttr).isinstance(val):
                if val is attr.NOTHING:
                    val = cls.Field(env=_field_env_key(cls.__openllm_model_name__, attr_name))
                else:
                    val = cls.Field(default=val, env=_field_env_key(cls.__openllm_model_name__, attr_name))
            these[attr_name] = val
        unannotated = ca_names - annotated_names
        if len(unannotated) > 0:
            missing_annotated = sorted(unannotated, key=lambda n: t.cast("_CountingAttr[t.Any]", cd.get(n)).counter)
            raise openllm.exceptions.MissingAnnotationAttributeError(
                f"The following field doesn't have a type annotation: {missing_annotated}"
            )

        cls.__openllm_accepted_keys__ = set(these.keys()) | {
            a.name for a in attr.fields(cls.__openllm_generation_class__)
        }

        # 'generation_config' is a special fields that wraps the GenerationConfig class
        # which is handled in _make_assignment_script
        these["generation_config"] = cls.Field(
            default=cls.__openllm_generation_class__(),
            description=inspect.cleandoc(cls.__openllm_generation_class__.__doc__ or ""),
            type=GenerationConfig,
        )

        # Generate the base __attrs_attrs__ transformation here.
        attrs, base_attrs, base_attr_map = _transform_attrs(
            cls,  # the current class
            these,  # the parsed attributes we previous did
            False,  # disable auto_attribs, since we already handle these
            False,  # disable kw_only
            True,  # collect_by_mro
            field_transformer=_make_env_transformer(cls, cls.__openllm_model_name__),
        )
        _weakref_slot = True  # slots = True
        _base_names = {a.name for a in base_attrs}
        _attr_names = tuple(a.name for a in attrs)
        _has_pre_init = bool(getattr(cls, "__attrs_pre_init__", False))
        _has_post_init = bool(getattr(cls, "__attrs_post_init__", False))
        # the protocol for attrs-decorated class
        cls.__attrs_attrs__ = attrs
        # generate a __attrs_init__ for the subclass, since we will
        # implement a custom __init__
        cls.__attrs_init__ = codegen.add_method_dunders(
            cls,
            _make_init(
                cls,  # cls (the attrs-decorated class)
                attrs,  # tuple of attr.Attribute of cls
                _has_pre_init,  # pre_init
                _has_post_init,  # post_init
                False,  # frozen
                True,  # slots
                False,  # cache_hash
                base_attr_map,  # base_attr_map
                False,  # is_exc (check if it is exception)
                None,  # cls_on_setattr (essentially attr.setters)
                attrs_init=True,  # whether to create __attrs_init__ instead of __init__
            ),
        )
        # __repr__ function with the updated fields.
        cls.__repr__ = codegen.add_method_dunders(cls, _make_repr(cls.__attrs_attrs__, None, cls))
        # Traverse the MRO to collect existing slots
        # and check for an existing __weakref__.
        existing_slots: DictStrAny = {}
        weakref_inherited = False
        for base_cls in cls.__mro__[1:-1]:
            if base_cls.__dict__.get("__weakref__", None) is not None:
                weakref_inherited = True
            existing_slots.update({name: getattr(base_cls, name) for name in getattr(base_cls, "__slots__", [])})

        if (
            _weakref_slot
            and "__weakref__" not in getattr(cls, "__slots__", ())
            and "__weakref__" not in _attr_names
            and not weakref_inherited
        ):
            _attr_names += ("__weakref__",)

        # We only add the names of attributes that aren't inherited.
        # Setting __slots__ to inherited attributes wastes memory.
        slot_names = [name for name in _attr_names if name not in _base_names]
        # There are slots for attributes from current class
        # that are defined in parent classes.
        # As their descriptors may be overridden by a child class,
        # we collect them here and update the class dict
        reused_slots = {
            slot: slot_descriptor for slot, slot_descriptor in existing_slots.items() if slot in slot_names
        }
        # __openllm_extras__ holds additional metadata that might be usefule for users, hence we add it to slots
        slot_names = [name for name in slot_names if name not in reused_slots] + ["__openllm_extras__"]
        cls.__slots__ = tuple(slot_names)
        # Finally, resolve the types
        if getattr(cls, "__attrs_types_resolved__", None) != cls:
            # NOTE: We will try to resolve type here, and cached it for faster use
            # It will be about 15-20ms slower comparing not resolving types.
            globs: DictStrAny = {"t": t, "typing": t, "Constraint": Constraint}
            if cls.__module__ in sys.modules:
                globs.update(sys.modules[cls.__module__].__dict__)
            attr.resolve_types(cls.__openllm_generation_class__, globalns=globs)

            cls = attr.resolve_types(cls, globalns=globs)
        # the hint cache for easier access
        cls.__openllm_hints__ = {
            f.name: f.type for ite in map(attr.fields, (cls, cls.__openllm_generation_class__)) for f in ite
        }
        cls = __llm_config_transform__(cls)

    def __setattr__(self, attr: str, value: t.Any):
        if attr in _reserved_namespace:
            raise ForbiddenAttributeError(
                f"{attr} should not be set during runtime "
                f"as these value will be reflected during runtime. "
                f"Instead, you can create a custom LLM subclass {self.__class__.__name__}."
            )

        super().__setattr__(attr, value)

    def __init__(
        self,
        *,
        generation_config: DictStrAny | None = None,
        __openllm_extras__: DictStrAny | None = None,
        **attrs: t.Any,
    ):
        # create a copy of the keys as cache
        _cached_keys = tuple(attrs.keys())

        _generation_cl_dict = attr.fields_dict(self.__openllm_generation_class__)
        if generation_config is None:
            generation_config = {k: v for k, v in attrs.items() if k in _generation_cl_dict}
        else:
            config_merger.merge(generation_config, {k: v for k, v in attrs.items() if k in _generation_cl_dict})

        for k in _cached_keys:
            if k in generation_config or attrs.get(k) is None:
                del attrs[k]
        _cached_keys = tuple(k for k in _cached_keys if k in attrs)

        self.__openllm_extras__ = first_not_none(__openllm_extras__, default={})
        config_merger.merge(
            self.__openllm_extras__, {k: v for k, v in attrs.items() if k not in self.__openllm_accepted_keys__}
        )

        for k in _cached_keys:
            if k in self.__openllm_extras__:
                del attrs[k]
        _cached_keys = tuple(k for k in _cached_keys if k in attrs)

        if DEBUG:
            logger.info(
                "Creating %s with the following attributes: %s, generation_config=%s",
                self.__class__.__name__,
                _cached_keys,
                generation_config,
            )

        # The rest of attrs should only be the attributes to be passed to __attrs_init__
        self.__attrs_init__(generation_config=self["generation_class"](**generation_config), **attrs)

    def __getitem__(self, item: str | t.Any) -> t.Any:
        """Allowing access LLMConfig as a dictionary. The order will always evaluate as

        __openllm_*__ > self.key > __openllm_generation_class__ > __openllm_extras__

        This method is purely for convenience, and should not be used for performance critical code.
        """
        if not isinstance(item, str):
            raise TypeError(f"LLM only supports string indexing, not {item.__class__.__name__}")
        if item in _reserved_namespace:
            raise ForbiddenAttributeError(
                f"'{item}' is a reserved namespace for {self.__class__} and should not be access nor modified."
            )
        internal_attributes = f"__openllm_{item}__"
        if hasattr(self, internal_attributes):
            return getattr(self, internal_attributes)
        elif hasattr(self, item):
            return getattr(self, item)
        elif hasattr(self.__openllm_generation_class__, item):
            return getattr(self.generation_config, item)
        elif item in self.__openllm_extras__:
            return self.__openllm_extras__[item]
        else:
            raise KeyError(item)

    def __getattribute__(self, item: str) -> t.Any:
        if item in _reserved_namespace:
            raise ForbiddenAttributeError(
                f"'{item}' belongs to a private namespace for {self.__class__} and should not be access nor modified."
            )
        return _object_getattribute.__get__(self)(item)

    def model_dump(self, flatten: bool = False, **_: t.Any):
        dumped = bentoml_cattr.unstructure(self)
        if flatten:
            generation_config = dumped.pop("generation_config")
            dumped.update(generation_config)
        return dumped

    def model_dump_json(self, **kwargs: t.Any):
        return orjson.dumps(self.model_dump(**kwargs))

    @classmethod
    def model_construct_env(cls, **attrs: t.Any) -> t.Self:
        """A helpers that respect configuration values that
        sets from environment variables for any given configuration class.
        """
        attrs = {k: v for k, v in attrs.items() if v is not None}

        model_config = cls.__openllm_env__.config

        env_json_string = os.environ.get(model_config, None)

        if env_json_string is not None:
            try:
                config_from_env = orjson.loads(env_json_string)
            except orjson.JSONDecodeError as e:
                raise RuntimeError(f"Failed to parse '{model_config}' as valid JSON string.") from e
        else:
            config_from_env = {}

        env_struct = bentoml_cattr.structure(config_from_env, cls)

        if "generation_config" in attrs:
            generation_config = attrs.pop("generation_config")
            if not LazyType(DictStrAny).isinstance(generation_config):
                raise RuntimeError(f"Expected a dictionary, but got {type(generation_config)}")
        else:
            generation_config = {
                k: v for k, v in attrs.items() if k in attr.fields_dict(env_struct.__openllm_generation_class__)
            }

        for k in tuple(attrs.keys()):
            if k in generation_config:
                del attrs[k]

        return attr.evolve(env_struct, generation_config=generation_config, **attrs)

    def model_validate_click(self, **attrs: t.Any) -> tuple[LLMConfig, DictStrAny]:
        """Parse given click attributes into a LLMConfig and return the remaining click attributes."""
        llm_config_attrs: DictStrAny = {"generation_config": {}}
        key_to_remove: ListStr = []

        for k, v in attrs.items():
            if k.startswith(f"{self['model_name']}_generation_"):
                llm_config_attrs["generation_config"][k[len(self["model_name"] + "_generation_") :]] = v
                key_to_remove.append(k)
            elif k.startswith(f"{self['model_name']}_"):
                llm_config_attrs[k[len(self["model_name"] + "_") :]] = v
                key_to_remove.append(k)

        return self.model_construct_env(**llm_config_attrs), {k: v for k, v in attrs.items() if k not in key_to_remove}

    @t.overload
    def to_generation_config(self, return_as_dict: t.Literal[False] = ...) -> transformers.GenerationConfig:
        ...

    @t.overload
    def to_generation_config(self, return_as_dict: t.Literal[True] = ...) -> DictStrAny:
        ...

    def to_generation_config(self, return_as_dict: bool = False) -> transformers.GenerationConfig | DictStrAny:
        config = transformers.GenerationConfig(**bentoml_cattr.unstructure(self.generation_config))
        return config.to_dict() if return_as_dict else config

    @classmethod
    @t.overload
    def to_click_options(
        cls, f: t.Callable[..., openllm.LLMConfig]
    ) -> F[P, ClickFunctionWrapper[..., openllm.LLMConfig]]:
        ...

    @classmethod
    @t.overload
    def to_click_options(cls, f: t.Callable[P, O_co]) -> F[P, ClickFunctionWrapper[P, O_co]]:
        ...

    @classmethod
    def to_click_options(cls, f: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        """
        Convert current model to click options. This can be used as a decorator for click commands.
        Note that the identifier for all LLMConfig will be prefixed with '<model_name>_*', and the generation config
        will be prefixed with '<model_name>_generation_*'.
        """
        for name, field in attr.fields_dict(cls.__openllm_generation_class__).items():
            ty = cls.__openllm_hints__.get(name)
            if t.get_origin(ty) is t.Union:
                # NOTE: Union type is currently not yet supported, we probably just need to use environment instead.
                continue
            f = dantic.attrs_to_options(name, field, cls.__openllm_model_name__, typ=ty, suffix_generation=True)(f)
        f = cog.optgroup.group(f"{cls.__openllm_generation_class__.__name__} generation options")(f)

        if len(cls.__openllm_accepted_keys__.difference(set(attr.fields_dict(cls.__openllm_generation_class__)))) == 0:
            # NOTE: in this case, the function is already a ClickFunctionWrapper
            # hence the casting
            return f

        # We pop out 'generation_config' as it is a attribute that we don't
        # need to expose to CLI.
        for name, field in attr.fields_dict(cls).items():
            ty = cls.__openllm_hints__.get(name)
            if t.get_origin(ty) is t.Union or name == "generation_config":
                # NOTE: Union type is currently not yet supported, we probably just need to use environment instead.
                continue
            f = dantic.attrs_to_options(name, field, cls.__openllm_model_name__, typ=ty)(f)

        return cog.optgroup.group(f"{cls.__name__} options")(f)


bentoml_cattr.register_unstructure_hook_factory(
    lambda cls: lenient_issubclass(cls, LLMConfig),
    lambda cls: make_dict_unstructure_fn(cls, bentoml_cattr, _cattrs_omit_if_default=False),
)


def structure_llm_config(data: DictStrAny, cls: type[LLMConfig]) -> LLMConfig:
    """
    Structure a dictionary to a LLMConfig object.

    Essentially, if the given dictionary contains a 'generation_config' key, then we will
    use it for LLMConfig.generation_config

    Otherwise, we will filter out all keys are first in LLMConfig, parse it in, then
    parse the remaining keys into LLMConfig.generation_config
    """
    if not LazyType(DictStrAny).isinstance(data):
        raise RuntimeError(f"Expected a dictionary, but got {type(data)}")

    generation_cls_fields = attr.fields_dict(cls.__openllm_generation_class__)
    cls_attrs = {
        k: v for k, v in data.items() if k in cls.__openllm_accepted_keys__ and k not in generation_cls_fields
    }
    if "generation_config" in data:
        generation_config = data.pop("generation_config")
        if not LazyType(DictStrAny).isinstance(generation_config):
            raise RuntimeError(f"Expected a dictionary, but got {type(generation_config)}")
        config_merger.merge(generation_config, {k: v for k, v in data.items() if k in generation_cls_fields})
    else:
        generation_config = {k: v for k, v in data.items() if k in generation_cls_fields}
    not_extras = list(cls_attrs) + list(generation_config)
    # The rest should be passed to extras
    data = {k: v for k, v in data.items() if k not in not_extras}

    return cls(generation_config=generation_config, __openllm_extras__=data, **cls_attrs)


bentoml_cattr.register_structure_hook_func(lambda cls: lenient_issubclass(cls, LLMConfig), structure_llm_config)

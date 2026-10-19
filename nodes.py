"""
ComfyUI SequenceCraft Pack — node definitions (V3 API).

  SCRandomPrompts        — {A|B|C} / __wildcard__ / ${var} resolution
  SCRandomPromptsDebug   — same, but errors shown in-node instead of crashing
  SCShowText             — display resolved text in-node
  SCImagePresetSlot      — pair one image with a variable preset (key: value format)
  SCImagePresetSequencePicker
                         — pick image+preset sequentially or at random
"""

from __future__ import annotations

import re
import random
from pathlib import Path
from typing import Any

from comfy_api.latest import ComfyExtension, io, ui
from typing_extensions import override

from . import parser

_WILDCARDS_DIR = Path(__file__).parent / "wildcards"
_MAX_SLOTS = 8


# ---------------------------------------------------------------------------
# Preset helpers
# ---------------------------------------------------------------------------

def _preset_to_declarations(preset_text: str) -> str:
    """Convert 'key: value' lines to ${key=!value} declaration strings.

    Format:
      outfit: {pleated skirt|jeans|sundress}
      pose: skirt fluttering in the wind
      # this line is a comment
    """
    result = []
    for line in preset_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        result.append(f"${{{key}=!{value}}}")
    return "".join(result)


# ---------------------------------------------------------------------------
# Prompt nodes
# ---------------------------------------------------------------------------

class RandomPromptsNode(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SCRandomPrompts",
            display_name="Random Prompts",
            category="SequenceCraft",
            description=(
                "Resolve {A|B|C} variants, __wildcard__ tokens, and ${var} variables.\n"
                "\n"
                "Syntax:\n"
                "  {A|B|C}      — pick one option at random\n"
                "  {2::A|B}     — weighted pick (A is twice as likely)\n"
                "  __name__     — random value from wildcards/name.txt\n"
                "  ${var=!val}  — declare variable\n"
                "  ${var}       — use variable\n"
                "\n"
                "Connect 'declarations' from Image Preset Sequence Picker to\n"
                "inject per-image variables."
            ),
            search_aliases=["dynamic prompts", "random prompt", "wildcard"],
            inputs=[
                io.String.Input(
                    "declarations",
                    display_name="Declarations",
                    multiline=False,
                    dynamic_prompts=False,
                    default="",
                    tooltip="Variable declarations from Image Preset Sequence Picker (optional).",
                ),
                io.String.Input(
                    "text",
                    display_name="Prompt Template",
                    multiline=True,
                    dynamic_prompts=False,
                    default="",
                    tooltip="Prompt template. Use {A|B|C}, __wildcard__, ${var}.",
                ),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    control_after_generate=io.ControlAfterGenerate.randomize,
                    tooltip="Random seed.",
                ),
            ],
            outputs=[
                io.String.Output(
                    "text",
                    display_name="Resolved Prompt",
                    tooltip="The prompt with all variants, wildcards, and variables resolved.",
                ),
            ],
        )

    @classmethod
    @override
    def execute(cls, declarations: str, text: str, seed: int) -> io.NodeOutput:
        result = parser.generate(declarations + text, seed, _WILDCARDS_DIR)
        return io.NodeOutput(result)


class ShowTextNode(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SCShowText",
            display_name="Show Text",
            category="SequenceCraft",
            description="Display text in-node. Passes the input through for chaining.",
            is_output_node=True,
            inputs=[
                io.String.Input(
                    "text",
                    display_name="Text",
                    force_input=True,
                    tooltip="Text to display.",
                ),
            ],
            outputs=[
                io.String.Output(
                    "text",
                    display_name="Text",
                    tooltip="Same text passed through for further chaining.",
                ),
            ],
        )

    @classmethod
    @override
    def execute(cls, text: str) -> io.NodeOutput:
        return io.NodeOutput(text, ui=ui.PreviewText(text))


class RandomPromptsDebugNode(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SCRandomPromptsDebug",
            display_name="Random Prompts (Debug)",
            category="SequenceCraft",
            description=(
                "Debug variant of Random Prompts.\n"
                "Syntax errors are displayed in-node; the workflow keeps running."
            ),
            search_aliases=["dynamic prompts", "random prompt", "wildcard", "debug"],
            is_output_node=True,
            inputs=[
                io.String.Input(
                    "declarations",
                    display_name="Declarations",
                    multiline=False,
                    dynamic_prompts=False,
                    default="",
                ),
                io.String.Input(
                    "text",
                    display_name="Prompt Template",
                    multiline=True,
                    dynamic_prompts=False,
                    default="",
                ),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    control_after_generate=io.ControlAfterGenerate.randomize,
                ),
            ],
            outputs=[
                io.String.Output(
                    "text",
                    display_name="Resolved Prompt",
                    tooltip="Resolved prompt, or empty string on syntax error.",
                ),
            ],
        )

    @classmethod
    @override
    def execute(cls, declarations: str, text: str, seed: int) -> io.NodeOutput:
        full = declarations + text
        errors = parser.validate_syntax(full)
        if errors:
            msg = "Syntax errors:\n" + "\n".join(f"  • {e}" for e in errors)
            return io.NodeOutput("", ui=ui.PreviewText(msg))
        result = parser.generate(full, seed, _WILDCARDS_DIR)
        return io.NodeOutput(result, ui=ui.PreviewText(result))


# ---------------------------------------------------------------------------
# Sequence nodes
# ---------------------------------------------------------------------------

class ImagePresetSlotNode(io.ComfyNode):
    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="SCImagePresetSlot",
            display_name="Preset Slot",
            category="SequenceCraft",
            description=(
                "Pair an image with a variable preset.\n"
                "Connect outputs to a Sequence Picker slot."
            ),
            is_output_node=True,
            inputs=[
                io.Image.Input("image"),
                io.String.Input(
                    "preset",
                    multiline=True,
                    default="",
                    tooltip=(
                        "1行1変数の形式で書く: varname: value\n"
                        "値には {A|B|C} や __wildcard__ がそのまま使える\n"
                        "# から始まる行はコメント\n"
                        "\n"
                        "例:\n"
                        "  outfit: {pleated skirt|jeans|sundress}\n"
                        "  pose: skirt fluttering in the wind\n"
                        "  bg: park, cherry blossoms\n"
                        "  expression: {smiling|laughing|neutral}"
                    ),
                ),
            ],
            outputs=[
                io.Image.Output("image"),
                io.String.Output("preset"),
            ],
        )

    @classmethod
    @override
    def execute(cls, image, preset: str = "") -> io.NodeOutput:
        declarations = _preset_to_declarations(preset)
        var_names = re.findall(r'\$\{(\w+)=!', declarations)
        if var_names:
            preview = "Variables: " + ", ".join(var_names)
        else:
            preview = "(no variables defined)"
        return io.NodeOutput(image, preset, ui=ui.PreviewText(preview))


class ImagePresetSequencePickerNode(io.ComfyNode):
    _counters: dict[str, int] = {}

    @classmethod
    @override
    def define_schema(cls) -> io.Schema:
        inputs: list = [
            io.Combo.Input("mode", options=["Sequential", "Random"]),
        ]
        for i in range(1, _MAX_SLOTS + 1):
            inputs.append(io.Image.Input(f"image_{i}", optional=True))
            inputs.append(io.String.Input(
                f"preset_{i}",
                multiline=True,
                default="",
                optional=True,
            ))
        inputs.append(io.String.Input(
            "fallback_preset",
            multiline=True,
            default="",
            tooltip="Used when a connected slot has no preset text.",
        ))

        return io.Schema(
            node_id="SCImagePresetSequencePicker",
            display_name="Sequence Picker",
            category="SequenceCraft",
            description=(
                "Pick one image+preset pair sequentially or at random.\n"
                "Connect 'declarations' to the Random Prompts node."
            ),
            inputs=inputs,
            outputs=[
                io.Image.Output("image"),
                io.String.Output(
                    "declarations",
                    tooltip="Connect to Random Prompts 'declarations' input.",
                ),
            ],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    @override
    def execute(cls, mode: str, fallback_preset: str = "", **kwargs) -> io.NodeOutput:
        slots: list[tuple[Any, str]] = []
        for i in range(1, _MAX_SLOTS + 1):
            img = kwargs.get(f"image_{i}")
            if img is not None:
                preset = kwargs.get(f"preset_{i}", "") or fallback_preset
                slots.append((img, preset))

        if not slots:
            raise ValueError(
                "[SequenceCraft] No images connected to Image Preset Sequence Picker."
            )

        node_id = str(cls.hidden.unique_id) if cls.hidden and cls.hidden.unique_id else "default"

        if mode == "Sequential":
            counter = cls._counters.get(node_id, 0)
            idx = counter % len(slots)
            cls._counters[node_id] = counter + 1
        else:
            idx = random.randint(0, len(slots) - 1)

        selected_image, selected_preset = slots[idx]
        declarations = _preset_to_declarations(selected_preset)

        return io.NodeOutput(selected_image, declarations)


# ---------------------------------------------------------------------------
# Extension registration
# ---------------------------------------------------------------------------

class SequenceCraftExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [
            RandomPromptsNode,
            RandomPromptsDebugNode,
            ShowTextNode,
            ImagePresetSlotNode,
            ImagePresetSequencePickerNode,
        ]

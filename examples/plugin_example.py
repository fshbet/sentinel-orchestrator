"""A plugin: one capability, one tool, one validator.

Register it by module name:

    plugins:
      modules: ["examples.plugin_example"]

or by entry point in your own package:

    [project.entry-points."orchestrator.plugins"]
    my-plugin = "my_package.plugin"

A plugin receives a narrow registration surface, not the platform object, so it
cannot reach into execution state.
"""

from orchestrator.core.domain.models import Capability, CapabilityRequirements, ToolSpec
from orchestrator.validation.validators import ValidationContext, Validator, ValidationSpec


class ContainsEveryRequiredSection(Validator):
    """A domain-neutral shape check: every named section is present.

    Config: ``sections`` - a list of strings that must all appear in the output.
    """

    name = "has_sections"

    async def validate(
        self, spec: ValidationSpec, context: ValidationContext
    ):
        required = [str(s) for s in spec.config.get("sections", [])]
        text = context.output_text()
        missing = [section for section in required if section not in text]
        return self._result(
            spec,
            context,
            passed=not missing,
            message=(
                "every required section is present"
                if not missing
                else "missing sections: " + ", ".join(missing)
            ),
        )


def word_count(arguments: dict, context) -> dict:
    """Count words in the supplied text. Needs no permissions."""
    text = str(arguments.get("text", ""))
    return {"words": len(text.split()), "characters": len(text)}


def register(registry) -> None:
    """Called once at startup with the plugin registration surface."""
    registry.add_capability(
        Capability(
            id="document-shaping",
            description="Produces documents with a required section structure.",
            requirements=CapabilityRequirements(tools=["text.word_count"]),
        )
    )
    registry.add_tool(
        ToolSpec(
            id="text.word_count",
            name="word_count",
            description="Count the words and characters in a piece of text.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        word_count,
    )
    registry.add_validator(ContainsEveryRequiredSection())

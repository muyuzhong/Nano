from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass
class Tool:
    """The tool wire shape the model sees. execute() lives on agent.AgentTool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

    def __post_init__(self) -> None:
        self.parameters = copy.deepcopy(self.parameters)

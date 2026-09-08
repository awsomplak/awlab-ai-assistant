import instructor
from openai import OpenAI
from pydantic import BaseModel, Field

from ..config import settings
from .logger import logger as _logger

log = _logger.tool("llm_extractor")

class Observation(BaseModel):
    signature: str = Field(..., description="Unique key for the observation")
    value: str = Field(..., description="The observed convention or preference")
    source: str = Field(..., description="must be 'explicit', 'corrected', or 'behavioral'")

class Entity(BaseModel):
    entityType: str = Field(..., description="Type of the entity (e.g. concept, bug, pattern)")
    name: str = Field(..., description="Name of the entity")
    observations: list[str] = Field(..., description="List of observation texts associated with the entity")

class ExtractionResult(BaseModel):
    observations: list[Observation] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)

def extract_memory(content: str) -> dict:
    if not settings.llm_enabled:
        return {"observations": [], "entities": []}

    try:
        client = instructor.from_openai(
            OpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key or "sk-dummy",
            )
        )
        result = client.chat.completions.create(
            model=settings.llm_model,
            response_model=ExtractionResult,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract observations and entities from the provided text to build a memory graph. "
                        "Return a structured list of observations and entities."
                    )
                },
                {"role": "user", "content": content},
            ],
            max_retries=2,
        )
        return result.model_dump()
    except Exception as e:
        log.error(f"LLM extraction failed: {e}")
        return {"observations": [], "entities": []}

from sqlalchemy import text

from dumpbot.ai.schemas import Extraction, extraction_json_schema
from dumpbot.services.settings_repo import load_prefs, save_prefs


def test_extraction_schema_builds():
    schema = extraction_json_schema()
    assert schema["additionalProperties"] is False
    e = Extraction.model_validate(
        {
            "intent": "save",
            "items": [
                {
                    "kind": "purchase",
                    "title": "Купить наушники",
                    "tags": ["#Электроника", "электроника"],
                    "when": {"text": "в обед", "time": "lunch"},
                }
            ],
        }
    )
    assert e.items[0].tags == ["электроника"]


async def test_schema_and_prefs_roundtrip(session):
    p = await load_prefs(session, 42)
    assert p.tz == "Europe/Warsaw"
    p.soon_days = 3
    await save_prefs(session, 42, p)
    await session.commit()
    p2 = await load_prefs(session, 42)
    assert p2.soon_days == 3
    r = await session.execute(text("select to_tsvector('russian', 'Купить наушники buying headphones')"))
    assert "наушник" in r.scalar_one()

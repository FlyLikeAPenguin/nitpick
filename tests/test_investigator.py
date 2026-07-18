import json

from src.investigator import parse_claude_output


def test_parse_claude_output_extracts_result_and_usage() -> None:
    stdout = json.dumps(
        {
            "result": '```json\n{"root_cause": "database timeout"}\n```',
            "usage": {"input_tokens": 12, "output_tokens": 8},
            "total_cost_usd": 0.04,
        }
    )

    result, usage = parse_claude_output(stdout)

    assert result == {"root_cause": "database timeout"}
    assert usage == {"input_tokens": 12, "output_tokens": 8, "cost_usd": 0.04}

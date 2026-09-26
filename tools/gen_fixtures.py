import json
from pathlib import Path

SENTENCES = [
    "welcome back everyone let us talk about api gateways today",
    "a gateway sits between your clients and all your services",
    "it handles routing authentication and rate limiting in one place",
    "each incoming request enters through a single stable front door",
    "then the gateway forwards it to the right backend service",
    "responses travel back through the same gateway directly to you",
    "security policies like tokens and quotas all live right here",
    "that is why one gateway can simplify your whole stack",
]
STEP, DUR, START = 0.30, 0.28, 0.5
GAPS_AFTER = [0.4, 1.2, 0.4, 1.2, 0.4, 1.2, 0.4]

words = []
t = START
for b, sentence in enumerate(SENTENCES):
    parts = sentence.split()
    assert len(parts) == 10, (b, len(parts), sentence)
    for w in parts:
        words.append(
            {"i": len(words), "t": w, "s": round(t, 3), "e": round(t + DUR, 3)}
        )
        t += STEP
    t = round(t - STEP + DUR, 3) + (GAPS_AFTER[b] if b < len(GAPS_AFTER) else 0.0)
    t = round(t, 3)
source_dur = round(t + 0.5, 3)

transcript = {"words": words, "text": " ".join(w["t"] for w in words)}

captions = [
    {"from_word": b * 10, "to_word": b * 10 + 9, "emphasis": e}
    for b, e in zip(range(8), [[8], [], [24], [], [42], [], [65], []])
]
draft = {
    "visuals": [
        {
            "kind": "icon",
            "keyword": "database",
            "from_word": 10,
            "to_word": 19,
            "pos": "top-right",
            "zoom": False,
        },
        {
            "kind": "icon",
            "keyword": "shield",
            "from_word": 40,
            "to_word": 49,
            "pos": "top-left",
            "zoom": False,
        },
        {
            "kind": "icon",
            "keyword": "route",
            "from_word": 70,
            "to_word": 79,
            "pos": "top",
            "zoom": True,
        },
    ],
    "captions": captions,
    "zoom_at_words": [14],
}

out = Path(__file__).parent.parent / "vedit" / "fixtures"
out.mkdir(exist_ok=True)
(out / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")
(out / "draft.json").write_text(json.dumps(draft, indent=2), encoding="utf-8")
print("words:", len(words), "source_dur:", source_dur)

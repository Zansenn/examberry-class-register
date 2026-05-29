"""
Read-only spike: dump Streak's structure so we can confirm our assumptions
(box = student, stage = class, one pipeline per year group / class type) before
building anything that writes.

Makes only GET calls — does not touch the CRM. Run:  python streak_explore.py
"""

from collections import Counter

from streak_client import StreakClient


def main():
    client = StreakClient()

    pipelines = client.list_pipelines()
    print(f"Pipelines visible to this key: {len(pipelines)}\n")

    for p in pipelines:
        stages = p.get("stages", {})  # stageKey -> {name, ...}
        print("=" * 70)
        print(f"PIPELINE  {p['name']}   (key={p['key']})")
        print(f"  stages: {len(stages)}")

        boxes = client.list_boxes(p["key"])
        by_stage = Counter(b.get("stageKey") for b in boxes)
        print(f"  boxes (students): {len(boxes)}")
        print("  --- students per stage (class) ---")

        # Preserve the pipeline's own stage ordering where available.
        order = p.get("stageOrder") or list(stages.keys())
        for sk in order:
            name = stages.get(sk, {}).get("name", "(unnamed)")
            print(f"    [{by_stage.get(sk, 0):>3}]  {name}   (stageKey={sk})")

        unplaced = [b for b in boxes if b.get("stageKey") not in stages]
        if unplaced:
            print(f"    [{len(unplaced):>3}]  (boxes with no/unknown stage)")

        # Show a couple of sample boxes so we can see what identity fields exist
        # (name, emails, etc.) for the per-enrolment vs per-student question.
        for b in boxes[:2]:
            keys = sorted(k for k in b.keys() if not k.startswith("_"))
            print(f"    sample box: name={b.get('name')!r}  fields={keys}")
        print()


if __name__ == "__main__":
    main()

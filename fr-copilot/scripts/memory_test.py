"""Memory test: short-term (same session follow-up) + long-term rep preference used in a NEW session."""
import sys, time, uuid
import boto3
sys.path.insert(0, "scripts")
from e2e_test import outputs, login, chat, check, summary  # noqa: E402

o = outputs(); base = o["SiteUrl"]; tok = login(o)
s1 = f"{uuid.uuid4()}-mem1"
_, ev, t, secs, _ = chat(base, tok, "Remember this for all my future chats: I always prefer the income-replacement method "
                         "for life insurance needs, and I like brief answers in bullet points.", s1)
print("pref ack:", t[:200].replace("\n", " "))
_, ev, t, secs, _ = chat(base, tok, "Does C-007 have a coverage gap?", s1)
_, ev2, t2, secs2, _ = chat(base, tok, "What if she retired at 67 instead of 65?", s1)
tools2 = [(e["tool"], e.get("input", {}).get("retirement_age")) for e in ev2 if e.get("type") == "tool_start"]
print("STM follow-up tools:", tools2)
check("STM: follow-up understood 'she' = C-007 and re-projected at 67",
      any(tn == "retirement_projection" and ra == 67 for tn, ra in tools2), t2[:150].replace("\n", " "))

mem = boto3.client("bedrock-agentcore", region_name="us-east-1")
recs = []
for _ in range(24):  # long-term extraction is asynchronous (typically ~1 min)
    recs = mem.list_memory_records(memoryId=o["MemoryId"], namespace="/preferences/rep-jordan").get("memoryRecordSummaries", [])
    if recs:
        break
    time.sleep(10)
for r in recs:
    print("LTM record:", r["content"].get("text", "")[:200])
check("LTM: rep preference extracted to /preferences/rep-jordan", bool(recs))

s2 = f"{uuid.uuid4()}-mem2"
_, ev3, t3, _, _ = chat(base, tok, "Does C-007 have a life insurance coverage gap?", s2)
methods = [e.get("input", {}).get("method") for e in ev3 if e.get("type") == "tool_start" and e["tool"] == "life_insurance_needs"]
print("new-session methods:", methods)
check("LTM: new session applies income-replacement preference", any(m and "income" in m.lower() for m in methods))
summary()

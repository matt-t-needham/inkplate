"""Talking-point question bank + deterministic selection.

The bank is curated here rather than fetched: online question APIs are flaky,
mostly icebreaker-grade, and this list can be tuned to taste. Add/remove
freely — selection stays stable within a slot regardless of bank edits that
day (it's seeded by the date), and nothing repeats until the whole bank has
been shown once.

House style, so additions match:
- One plain, direct question, the way you'd ask it across the breakfast
  table. No setup statement followed by a tag ("...Convinced?", "Push
  back.", "Change my mind.") — that reads as an essay prompt, not a chat.
- Concrete and grounded over grand. If it sounds like it wants to be deep,
  it's trying too hard; ask the ordinary version.
- Short enough to read from across the room: four wrapped lines at most.

Categories are kept deliberately even (30 each) so no one flavour dominates
the rotation. They are grouping only — the panel shows the question alone.

BANK is the shipped default. The control pane can edit, add, and veto
questions; once it does, the live bank is `data/questions.json` and BANK is
only the seed (and the "reset to defaults" target). Changing the bank's size
reshuffles the cycle, so the current pick moves on — which is what a veto
wants anyway. Editing a question's wording in place keeps the pick put.
"""

import json
import os
import random
from datetime import date

import config

BANK = [
    # ── personal ──────────────────────────────────────────────────────────────
    ("personal", "What did you believe five years ago that you've since quietly dropped?"),
    ("personal", "What's the smallest version of a year off you could actually afford?"),
    ("personal", "Which compliment have you never forgotten, and why that one?"),
    ("personal", "What skill do you admire in other people but have never tried yourself?"),
    ("personal", "When did you last change your mind about something that mattered?"),
    ("personal", "Which of your habits would surprise your teenage self the most?"),
    ("personal", "What's the best advice you ignored, and were you right to ignore it?"),
    ("personal", "If you could relive one ordinary day, which would it be?"),
    ("personal", "What are you deliberately doing badly because fixing it isn't worth it?"),
    ("personal", "Who do you owe a phone call, and what's actually stopping you?"),
    ("personal", "Which part of your daily routine would you defend hardest?"),
    ("personal", "What's the longest you've stayed in something you knew wasn't right for you?"),
    ("personal", "Which fictional character's mistake has stayed with you?"),
    ("personal", "What do you want to be doing when you're eighty?"),
    ("personal", "What small kindness from someone else do you still remember?"),
    ("personal", "How do you think your friends describe you when you're not there?"),
    ("personal", "When did you last make something with your hands?"),
    ("personal", "What would you happily be mediocre at for the rest of your life?"),
    ("personal", "Which place do you think about more than makes sense?"),
    ("personal", "Is being busy something that happens to you, or something you choose?"),
    ("personal", "What are you better at than you let on?"),
    ("personal", "What advice do you give often but rarely take?"),
    ("personal", "Would you rather be respected or liked?"),
    ("personal", "Is there a version of your life you almost chose that you still think about?"),
    ("personal", "What have you edited out of a time you remember fondly?"),
    ("personal", "What are you proudest of that would never appear on a CV?"),
    ("personal", "What personal rule do you keep that nobody else knows about?"),
    ("personal", "Which topic can you never be casual about, and where did that come from?"),
    ("personal", "What did your parents get right that you only appreciated later?"),
    ("personal", "What would you do with an extra hour every day?"),

    # ── philosophy ────────────────────────────────────────────────────────────
    ("philosophy", "Is there a real difference between a very good imitation of caring and caring?"),
    ("philosophy", "If your memories were swapped one by one for accurate recordings, would you still be you?"),
    ("philosophy", "Would you choose a happy illusion over a painful truth?"),
    ("philosophy", "Does a promise to someone who has died still bind you?"),
    ("philosophy", "If everything is determined, should that change how we treat each other?"),
    ("philosophy", "Can you know something you could never explain to anyone else?"),
    ("philosophy", "If a perfect copy of you existed, which of you would be the real one?"),
    ("philosophy", "Is boredom about you, or about the thing you're doing?"),
    ("philosophy", "Would you take a pill that made you permanently content?"),
    ("philosophy", "How do you decide which traditions are worth keeping?"),
    ("philosophy", "Can you be harmed by something you never find out about?"),
    ("philosophy", "Does explaining awe scientifically make it any less awe-inspiring?"),
    ("philosophy", "Is regret ever useful?"),
    ("philosophy", "Is free will anything more than not yet knowing what you'll do?"),
    ("philosophy", "Is it comforting or depressing that nothing you do will matter in a thousand years?"),
    ("philosophy", "How much of what you find beautiful were you taught to like?"),
    ("philosophy", "Day to day, which is more useful: honesty or tact?"),
    ("philosophy", "Is death bad for the person who dies, or only for the people left behind?"),
    ("philosophy", "Is your sense of self a story you tell, or is there something underneath it?"),
    ("philosophy", "How much of your success comes down to luck?"),
    ("philosophy", "Is maths discovered or invented?"),
    ("philosophy", "What would it take to convince you that a machine understands something?"),
    ("philosophy", "Is forgetting a flaw in memory, or part of what makes it work?"),
    ("philosophy", "Is the past still real once it's over?"),
    ("philosophy", "When you want something you know is bad for you, which part of you is really you?"),
    ("philosophy", "Can two people who speak different languages ever fully understand each other?"),
    ("philosophy", "If a country's laws, land and people all change, is it still the same country?"),
    ("philosophy", "Is anything good for its own sake, or only for what it leads to?"),
    ("philosophy", "Can you be wrong about whether you're happy?"),
    ("philosophy", "Should you trust your gut on moral questions?"),

    # ── politics ──────────────────────────────────────────────────────────────
    ("politics", "Should there be an upper age limit for holding political office?"),
    ("politics", "Would choosing some legislators by lottery, like a jury, work better?"),
    ("politics", "Are you responsible for policies you voted against?"),
    ("politics", "Should voting be compulsory?"),
    ("politics", "Should a government ever ban speech that's true but dangerous?"),
    ("politics", "Is the nation-state the right size for problems like climate change and pandemics?"),
    ("politics", "Should a democracy ban parties that want to end democracy?"),
    ("politics", "Why is a land-value tax so popular with economists and so rare in practice?"),
    ("politics", "Should sixteen-year-olds be allowed to vote?"),
    ("politics", "What's the real difference between lobbying and bribery?"),
    ("politics", "Who should speak for future generations, who can't vote yet?"),
    ("politics", "When a court blocks a popular policy, is that democracy working or failing?"),
    ("politics", "Should political ads be allowed to target individual voters?"),
    ("politics", "What's the strongest case for open borders?"),
    ("politics", "Are referendums a good way to settle big questions?"),
    ("politics", "How much should elected politicians defer to experts?"),
    ("politics", "Should cities have more power relative to national governments?"),
    ("politics", "Do term limits help or hurt good government?"),
    ("politics", "Is a written constitution better than an unwritten one?"),
    ("politics", "Should protest be allowed to cause serious disruption?"),
    ("politics", "Why do people want a strong leader until they get one?"),
    ("politics", "Should public money fund the arts, sport, both or neither?"),
    ("politics", "Is there a better way than voting to find out what people want?"),
    ("politics", "Does a free press still work if most people don't trust it?"),
    ("politics", "Is bureaucracy the price of fairness, or just inefficiency?"),
    ("politics", "Would a year of national service be good for a country?"),
    ("politics", "How can a democracy invest in things that pay off after the next election?"),
    ("politics", "Can borders be morally arbitrary and still be necessary?"),
    ("politics", "Should politicians be paid much more, or much less?"),
    ("politics", "If you had one day in charge, which policy would you change first?"),

    # ── ethics (moral, medical, computing, global) ────────────────────────────
    ("ethics", "Does being rude to an AI say something about you, even if it can't be hurt?"),
    ("ethics", "Is it wrong to save one person you love over five strangers?"),
    ("ethics", "Is anonymous giving morally better than giving publicly?"),
    ("ethics", "Is letting someone die as bad as killing them?"),
    ("ethics", "Can you wrong someone just by thinking badly of them?"),
    ("ethics", "How much of your income do you owe to people poorer than you?"),
    ("ethics", "Is loyalty to someone who doesn't deserve it still a virtue?"),
    ("ethics", "Can you separate the art from the artist?"),
    ("ethics", "Is a kind lie still a lie?"),
    ("ethics", "If everyone softens the truth, does praise stop meaning anything?"),
    ("ethics", "Should two drunk drivers be punished differently if only one hits someone?"),
    ("ethics", "Is it selfish to have children partly so someone will care for you later?"),
    ("ethics", "When is forgiveness the wrong response?"),
    ("ethics", "Which of today's normal habits will future generations judge most harshly?"),
    ("ethics", "Should the amount of cash in a lost wallet affect whether you return it?"),
    ("ethics", "Can a competent adult refuse life-saving treatment if others depend on them?"),
    ("ethics", "Should organ donors be paid?"),
    ("ethics", "When a hospital runs out of beds, how should it decide who gets one?"),
    ("ethics", "If someone with dementia wants what their earlier self forbade, whose wishes count?"),
    ("ethics", "Is it ever acceptable for a doctor to prescribe a placebo?"),
    ("ethics", "Who should be paid when an AI is trained on people's work?"),
    ("ethics", "Should engineers refuse to build things they think are harmful?"),
    ("ethics", "Should we rely on a system that works better if nobody can explain how?"),
    ("ethics", "When does a recommendation algorithm stop serving you and start exploiting you?"),
    ("ethics", "Should you always have the right to deal with a human rather than a machine?"),
    ("ethics", "Is an AI companion that eases someone's loneliness doing real good?"),
    ("ethics", "Should encryption have a back door for the police?"),
    ("ethics", "Do you owe more to a stranger overseas or to your neighbour?"),
    ("ethics", "Is it wrong to eat meat?"),
    ("ethics", "Should you report a friend who has broken the law?"),
]


MAX_TEXT = 300


def _bank_path():
    return config.DATA_DIR / "questions.json"


def bank() -> list[tuple[str, str]]:
    """The live bank: the pane's edited copy if one exists, else BANK."""
    try:
        rows = json.loads(_bank_path().read_text())
        out = [(str(c), str(t)) for c, t in rows if str(t).strip()]
        if out:
            return out
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, OSError):
        pass  # corrupt override: fall back to the shipped bank
    return list(BANK)


def save_bank(rows) -> list[tuple[str, str]]:
    """Validate and persist an edited bank (atomic). Raises ValueError."""
    clean = []
    for row in rows:
        if isinstance(row, dict):
            c, t = row.get("category", ""), row.get("text", "")
        else:
            c, t = row
        c, t = str(c).strip()[:40] or "misc", " ".join(str(t).split())
        if not t:
            continue
        if len(t) > MAX_TEXT:
            raise ValueError(f"question too long ({len(t)} > {MAX_TEXT}): {t[:40]}…")
        clean.append((c, t))
    if not clean:
        raise ValueError("the bank needs at least one question")
    p = _bank_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=1, ensure_ascii=False))
    os.replace(tmp, p)
    return clean


def reset_bank() -> None:
    _bank_path().unlink(missing_ok=True)


def slot_for(day: date, period_days: int = 1, offset: int = 0) -> int:
    """The content slot for a date: an integer that advances once every
    `period_days`, plus the pane's manual cycle `offset`."""
    return day.toordinal() // max(1, int(period_days)) + offset


def question_for(day: date, offset: int = 0, period_days: int = 1) -> dict:
    """Deterministic pick: a per-cycle shuffle guarantees every question
    appears once before any repeats, and the same slot always yields the same
    question (renders are reproducible). `period_days` sets how often it
    changes (1 = daily); `offset` advances the sequence — the control pane's
    "next question" button."""
    i = index_for(day, offset, period_days)
    category, text = bank()[i]
    return {"category": category, "text": text}


def index_for(day: date, offset: int = 0, period_days: int = 1) -> int:
    """Index into bank() of the question question_for() picks."""
    n = len(bank())
    ordinal = slot_for(day, period_days, offset)
    cycle, pos = divmod(ordinal, n)
    perm = random.Random(cycle).sample(range(n), n)
    return perm[pos]

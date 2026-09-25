"""Talking-point question bank + deterministic selection.

The bank is curated here rather than fetched: online question APIs are flaky,
mostly icebreaker-grade, and this list can be tuned to taste. Add/remove
freely — selection stays stable within a slot regardless of bank edits that
day (it's seeded by the date), and nothing repeats until the whole bank has
been shown once.

House style, so additions match:
- Casual, spoken. Nothing that sounds like a seminar prompt — no "discuss",
  no "defend your position", no jargon a friend wouldn't use out loud.
- Prefer taking a side and inviting the reader to knock it down over asking
  neutrally. "X is true — convinced?" beats "Is X true?".
- Short enough to read from across the room: four wrapped lines at most.

Categories are kept deliberately even (30 each) so no one flavour dominates
the rotation. They are grouping only — the panel shows the question alone.
"""

import random
from datetime import date

BANK = [
    # ── personal ──────────────────────────────────────────────────────────────
    ("personal", "Most of what you believed five years ago you've quietly dropped, and you couldn't say when. What went?"),
    ("personal", "If money weren't the problem you'd take a year off — so why not take the smaller version you can actually afford?"),
    ("personal", "The compliment you still remember probably wasn't the biggest one. Why did that one stick?"),
    ("personal", "There's a skill you admire in other people and have never once attempted. What's actually stopping you?"),
    ("personal", "Changing your mind about something that mattered is rare. When did you last manage it?"),
    ("personal", "You behave differently when nobody will ever see the result, and that version is arguably the real one. Fair?"),
    ("personal", "Your teenage self would be more surprised by your habits than by your job. Which habit wins?"),
    ("personal", "The best advice you ever got, you ignored. Was ignoring it the right call?"),
    ("personal", "Given one day to relive, everyone picks a milestone. An ordinary Tuesday would tell you more — which one?"),
    ("personal", "Some things you're getting wrong on purpose, because fixing them isn't worth it. Which ones?"),
    ("personal", "You owe someone a phone call, and the reason you haven't made it isn't time. So what is it?"),
    ("personal", "If someone tried to take one piece of your daily routine, which bit would you fight hardest for?"),
    ("personal", "Staying too long in the wrong thing is normal, not stupid. What's your record?"),
    ("personal", "A made-up character's failure has stayed with you longer than most real ones. Whose?"),
    ("personal", "Nobody plans for eighty. What do you want to be doing then?"),
    ("personal", "Someone once did you a small kindness they've completely forgotten. You haven't. What was it?"),
    ("personal", "Your friends describe you in one sentence when you're not there. What do you hope it is, and what do you suspect?"),
    ("personal", "Making something with your hands beats consuming something with your eyes. When did you last do it?"),
    ("personal", "You'd enjoy being permanently mediocre at something if you let yourself. What would it be?"),
    ("personal", "You think about a particular place far more than makes sense. Where — and what's it standing in for?"),
    ("personal", "Being busy is mostly a choice people describe as a circumstance. True of you?"),
    ("personal", "You're better at something than you let on. What, and why keep it quiet?"),
    ("personal", "The advice you hand out most often is advice you don't take. Which is it?"),
    ("personal", "Everyone says they'd rather be respected than liked, and most of them mean the opposite. Which are you?"),
    ("personal", "There's a version of your life you almost chose. Do you still think about it, or has it gone quiet?"),
    ("personal", "Nostalgia is mostly editing. What have you edited out of a time you remember fondly?"),
    ("personal", "The thing you're proudest of isn't on your CV. What is it?"),
    ("personal", "You keep a rule nobody else knows about. What is it?"),
    ("personal", "Everyone has one topic they can't be casual about. What's yours, and where did it come from?"),
    ("personal", "You'd hesitate, but you'd pick one: which sense would you give up to sharpen the others?"),

    # ── philosophy ────────────────────────────────────────────────────────────
    ("philosophy", "Swap your memories for accurate recordings one at a time and eventually you stop being you. Where's the line?"),
    ("philosophy", "A contented illusion beats a painful truth, and the people who deny it have never been offered the choice. Wrong?"),
    ("philosophy", "A promise to someone who has died still binds you — nobody's checking, which is exactly why it counts. Agree?"),
    ("philosophy", "If everything is determined, almost nothing about how we should treat each other changes. Why would it?"),
    ("philosophy", "There are things you know and could never explain to anyone. Doesn't that make them a little suspect?"),
    ("philosophy", "A perfect copy of you would insist it's you, and its claim would be as good as yours. Uncomfortable?"),
    ("philosophy", "“Why is there something rather than nothing” might not be a real question at all. Is it?"),
    ("philosophy", "Boredom tells you about your attention, not about the world. Or is the world genuinely dull sometimes?"),
    ("philosophy", "A pill that left you permanently content with your life would be a surrender, not a win. Would you take it?"),
    ("philosophy", "A tradition is a habit nobody has examined lately. What separates the ones worth keeping?"),
    ("philosophy", "The past is as real as the present, we just can't get back to it. Right, or word games?"),
    ("philosophy", "Wanting something you know is bad for you isn't one want, it's two having a fight. Does that describe it?"),
    ("philosophy", "Perfect translation is impossible, so two people in different languages are always approximating. What are they really doing?"),
    ("philosophy", "A country whose laws, land and people have all changed is a different country — so it shouldn't inherit the old one's debts. Or should it?"),
    ("philosophy", "Maths is discovered, not invented, and any alien civilisation would have our primes. Convinced?"),
    ("philosophy", "You'd never accept that a machine understands anything. What would it have to do to move you?"),
    ("philosophy", "Forgetting isn't a flaw in memory — it's most of what makes a life liveable. Agree?"),
    ("philosophy", "You can't be harmed by something you never find out about. Can you?"),
    ("philosophy", "Awe is your brain briefly failing to make sense of something. Does putting it that way ruin it?"),
    ("philosophy", "Regret points at a past you can't reach, which makes it useless, and everyone has it anyway. Why?"),
    ("philosophy", "Free will is mostly the feeling of not knowing yet what you'll do. Is there more to it?"),
    ("philosophy", "Nothing you do will matter in a thousand years, and that's a relief rather than a tragedy. Sound right?"),
    ("philosophy", "Beauty is mostly agreement. Is anything left once you subtract what you were taught to like?"),
    ("philosophy", "You can't step in the same river twice — but you can't step in a different one either. Which half is wrong?"),
    ("philosophy", "Consciousness might be the least mysterious thing about you, and we just can't see it from the inside. Plausible?"),
    ("philosophy", "Honesty is overrated as a virtue; tact does more good on an average day. Push back."),
    ("philosophy", "There's no real difference between a very good imitation of caring and caring. Is there?"),
    ("philosophy", "Death is bad for the living, not for the dead. Does knowing that help at all?"),
    ("philosophy", "Your identity is a story you tell and quietly keep editing. Is there anything underneath it?"),
    ("philosophy", "Luck deserves far more credit than effort in most successes. How much of yours?"),

    # ── politics ──────────────────────────────────────────────────────────────
    ("politics", "There should be an upper age limit on holding office. If not age, what would you use instead?"),
    ("politics", "Filling some seats by lottery, the way juries work, would fix more than it broke. Would it?"),
    ("politics", "A region that leaves still owes the country it left something. What, exactly?"),
    ("politics", "You're partly answerable for policies you voted against, because you pocket the system's wins too. Fair?"),
    ("politics", "Compulsory voting turns indifference into noise. Is a forced ballot worse information than an empty one?"),
    ("politics", "A government shouldn't ban speech for being destabilising, even when it's true and dangerous. Any exceptions?"),
    ("politics", "The nation-state is the wrong size for climate, pandemics and AI. What's the right size?"),
    ("politics", "A democracy shouldn't tolerate parties that want to end democracy. Where does that rule stop?"),
    ("politics", "Taxing land instead of work is the rare idea economists across the spectrum agree on. So why is it untouchable?"),
    ("politics", "Sixteen-year-olds live with the results longest, so they should get the vote. What's the real argument against?"),
    ("politics", "A lobbyist is a bribe with better paperwork. Can you state the difference cleanly?"),
    ("politics", "Future generations can't vote, so someone speaks for them. Who, and with how much weight?"),
    ("politics", "When a court blocks a popular policy, that's democracy working rather than failing. Convinced?"),
    ("politics", "Letting parties tailor what each voter sees is gerrymandering of the mind. Too strong?"),
    ("politics", "Make the best case for open borders you can, even though it isn't your position. How far does it get?"),
    ("politics", "A referendum isn't purer democracy, it's a way to dodge the work of deciding. Agree?"),
    ("politics", "Experts are unelected and sometimes wrong, and we should still mostly defer to them. Where's your limit?"),
    ("politics", "Cities should hold more formal power than they do, given that's where everyone lives. Who loses?"),
    ("politics", "Most political parties outlive the reason they were founded by decades. Name one that hasn't."),
    ("politics", "Term limits mainly hand power to lobbyists and staffers who never leave. Still worth it?"),
    ("politics", "A written constitution ages badly; an unwritten one bends to whoever's in charge. Pick your poison."),
    ("politics", "Protest only works when it's inconvenient, which is precisely when it gets banned. How do you square that?"),
    ("politics", "Most people want a strong leader right up until they get one. Sure you don't?"),
    ("politics", "Public money for the arts is easier to justify than public money for sport, and we fund it the other way round. Why?"),
    ("politics", "Voting is a terrible way to express what people want, and every alternative is worse. Is it?"),
    ("politics", "A free press nobody trusts does the same job as no free press at all. Overstated?"),
    ("politics", "Bureaucracy is what fairness looks like when it's slow. Or is it just slow?"),
    ("politics", "A year of civil service would do more for a country's cohesion than any policy currently on offer. Sell me the downside."),
    ("politics", "Every government underinvests in anything that pays off after the next election. Fixable, or just what democracy is?"),
    ("politics", "Borders are morally arbitrary and practically indispensable. Can both be true at once?"),

    # ── ethics (moral, medical, computing, global) ────────────────────────────
    ("ethics", "Being rude to a machine that convincingly asks you to stop says something about you, even though it can't suffer. Does it?"),
    ("ethics", "Everyone saves the person they love over five strangers. So what is impartial morality actually for?"),
    ("ethics", "Giving anonymously isn't morally better than giving publicly, it just looks humbler. Change my mind."),
    ("ethics", "Letting someone die when you could have cheaply saved them is nearly as bad as killing them. Nearly?"),
    ("ethics", "You can't wrong someone by thinking badly of them if it never shows. Sure about that?"),
    ("ethics", "Somewhere, giving more stops being a duty and becomes merely admirable. Where did you put that line, and why there?"),
    ("ethics", "Loyalty to someone who doesn't deserve it isn't a virtue, it's a habit. Too harsh?"),
    ("ethics", "A monstrous artist's work isn't tainted by the monster — the work doesn't know. Does that hold up?"),
    ("ethics", "Promising what you probably can't deliver is a lie you tell to be kind. Still a lie?"),
    ("ethics", "If everyone softens the truth to spare feelings, praise stops meaning anything. Real cost, or imaginary?"),
    ("ethics", "We owe the dead less than we pretend. How long should a promise outlive the person you made it to?"),
    ("ethics", "Two identical drunk drivers, one hits someone. Punishing them differently makes no sense, and we do it anyway. Why?"),
    ("ethics", "Having a child partly so someone will care for you later is honest rather than shameful. Uncomfortable?"),
    ("ethics", "Forgiveness is sometimes just refusing to take the wrong seriously. How do you tell those apart?"),
    ("ethics", "Eating meat is what our grandchildren will find hardest to excuse. Or is that just moral fashion?"),
    ("ethics", "You'd return a wallet holding ten pounds and think harder about one holding a thousand. Should the amount matter?"),
    ("ethics", "A competent adult can refuse a cheap, painless cure, even with people depending on them. Can they?"),
    ("ethics", "If artificial wombs worked, most of the abortion argument would have to be rebuilt from scratch. Would it?"),
    ("ethics", "Paying organ donors is banned, yet everyone else in the operating theatre gets paid. What's the principle?"),
    ("ethics", "Triage by life-years saved quietly puts the old and disabled last. Is there a better rule that doesn't?"),
    ("ethics", "Someone with dementia cheerfully wants what their own advance directive forbids. Which of them is the patient?"),
    ("ethics", "Doctors shouldn't prescribe placebos they know are placebos, even when honesty is what breaks the effect. Agree?"),
    ("ethics", "A model trained on everything people ever wrote owes somebody something, but nobody can say who or how much. Can you?"),
    ("ethics", "Engineers should be able to refuse work the way a structural engineer refuses an unsafe bridge. What would you refuse to build?"),
    ("ethics", "A system nobody can explain is still the right choice if it plainly works better. Is it?"),
    ("ethics", "A feed that learns outrage keeps you watching is serving you, right up until it's exploiting you. Where's that line?"),
    ("ethics", "You should be able to reach a human — in court, in hospital, at the benefits office — however good machines get. Absolute?"),
    ("ethics", "An AI companion that genuinely eases someone's loneliness is doing real good, even with nothing on the other end. Does the emptiness matter?"),
    ("ethics", "Encryption shields dissidents and predators equally and there's no third option. Which mistake would you rather make?"),
    ("ethics", "Rich countries owe more to a stranger overseas than to a neighbour, because the money goes fifty times further. Do they?"),
]


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
    n = len(BANK)
    ordinal = slot_for(day, period_days, offset)
    cycle, pos = divmod(ordinal, n)
    perm = random.Random(cycle).sample(range(n), n)
    category, text = BANK[perm[pos]]
    return {"category": category, "text": text}

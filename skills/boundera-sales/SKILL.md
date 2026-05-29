---
name: boundera-sales
description: Draft and improve Boundera sales replies, LinkedIn DMs, email follow-ups, Slack nudges, call scripts, objection handling, account routing, nurture messages, and next-step strategy for FedRAMP 20x, Rev. 5, KSI, SSP, POA&M, evidence, assessor, CSP, 3PAO, channel, Carahsoft, Red River, and GovTech SaaS conversations. Use when the user asks what to say to a prospect, how to handle a sales objection, whether to call/email/LinkedIn, how to nurture a relationship, how to map a target account, or how to move a stalled Boundera deal forward.
---

# Boundera Sales

## Core Workflow

When drafting or advising on a prospect conversation:

1. Pull real context first when available.
   - Search the DB for the lead, deal, campaign, messages, and local notes.
   - Prefer the latest actual thread over memory or generic sales advice.
   - If the user pasted fresh context, treat it as the newest source.

2. Classify the situation.
   - Reply drafting: prospect responded and needs an answer.
   - Objection handling: timing, role mismatch, not a fit, use email, send info, too busy, no budget, security concern, or unclear value.
   - Nurture: no active need, relationship should stay warm.
   - Account routing: find the right person or team.
   - Call prep: create opener, discovery questions, objection responses, and voicemail.
   - Deal inspection: identify single-threading, weak champion, missing buyer, no next step, or stop-contact risk.

3. Choose the right posture.
   - Respect explicit channel preferences immediately.
   - If they say stop contacting them on a channel, do not draft for that channel.
   - Do not "overcome" a soft no by arguing. Acknowledge, reduce friction, and preserve the relationship.
   - If they show interest, ask for one concrete next step.
   - If they are confused, simplify the ask before adding more product detail.

4. Draft short, direct copy.
   - Lead with acknowledgement.
   - Add one sentence of context or value.
   - Ask one clear question.
   - Keep LinkedIn and Slack conversational.
   - Keep email slightly more complete and forwardable.
   - Avoid "just checking in" unless paired with a new reason.

5. Give the user the ready-to-send version first.
   - Include optional alternates only when useful.
   - If they ask to update a temp file, write to the relevant local temp file.

## Context Retrieval

In the OpenOutreach repo, use Django ORM when the user names a person:

```bash
.venv/bin/python manage.py shell -c "from crm.models import Lead, Deal, Message; ..."
```

Look up by public identifier, first/last name, company, or email. Return:
- lead name, company, LinkedIn URL, email, ICP
- campaign owner and state
- message history ordered by `sent_at`
- any local notes under `local_notes/`

Common temp files:
- `local_notes/temp_linked_in_message.txt`
- `local_notes/temp_email_message.txt`

## Boundera Positioning

Default one-liner:

> Boundera helps software vendors move through FedRAMP authorization and continuous monitoring with less manual evidence work, gap tracking, POA&M management, SSP/KSI package generation, and assessor review friction.

Use "operating system for FedRAMP 20x" only when the recipient is warm enough or technical enough for category language. For confused prospects, use simpler language:

> We help vendors working through FedRAMP reduce the manual work around evidence, remediation, POA&Ms, and ongoing monitoring.

For terminology-sensitive FedRAMP claims, verify against official sources or use the `boundera-fedramp-copy` skill. Do not overclaim authorization outcomes, timelines, agency acceptance, or compliance guarantees.

## Persona Routing

Read `references/personas.md` when deciding angle by ICP or role.

Fast defaults:
- CSP/security operator: pain is authorization, cloud evidence, remediation, continuous monitoring.
- Advisor/vCISO/consultant: pain is delivery leverage, client readiness, evidence review, referral, repeatable workflow.
- 3PAO/assessor: pain is evidence review, traceability, assessor portal, SRTM/RAR/SAR-style outputs.
- Channel/partner: pain is vendor ecosystem, co-sell, referral, right internal owner.
- Federal SI/MSP: pain is customer projects, cloud/security delivery, federal modernization workflows.

## Objection Handling

Read `references/objections.md` for common response patterns.

Default formula:

1. Acknowledge the objection.
2. Interpret it in plain language.
3. Reduce the ask.
4. Ask one routing, timing, or feedback question.

Examples:
- Timing: "Totally understand. Sounds like timing is the issue, not necessarily the category."
- Not my role: "Makes sense. Who usually owns this conversation?"
- Send info: "Happy to. What would make it useful: product overview, partner angle, or technical workflow?"
- Use email only: "Noted. I will keep this to email going forward."
- Stop contact: "Understood. I will not contact you here again."

## Channel Guidance

LinkedIn:
- Best for warm, conversational nudges and relationship maintenance.
- Keep under 3 short paragraphs.
- Do not attach too much detail.
- If conversation started on LinkedIn, a light bump is acceptable unless they asked for email.

Email:
- Best for forwarded context, formal partner routing, technical detail, and calendar asks.
- Make it easy to forward internally.
- Include a crisp subject if drafting a full email.

Slack:
- Best for active pilots, sandbox follow-up, and collaborative relationships.
- Keep relationship-first. Avoid sounding like a sequence.

Phone:
- Use only when there is a legitimate reason: meeting booked, warm interaction, direct phone provided, event follow-up, or urgent coordination.
- Open with permission: "Do you have 30 seconds?"
- If calling as Arian after another cofounder chatted with the prospect, say that clearly.

## Stop Conditions

Recommend no further outreach on a channel when:
- They explicitly say stop contacting me here.
- They tell you to use email only.
- They gave a clear no and no permission to stay in touch.
- The conversation is becoming irritated, sarcastic, or corrective.
- The next message would be the third ask without new information.

If there is still possible value, move to the approved channel after a cooling-off period.

## References

- `references/personas.md`: Boundera ICPs, angles, and examples.
- `references/objections.md`: Objection and response patterns.
- `references/message-patterns.md`: Copy style, channel templates, call script structure.

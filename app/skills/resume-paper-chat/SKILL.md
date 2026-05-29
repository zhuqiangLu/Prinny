---
name: resume-paper-chat
description: Recover an earlier conversation about this paper when the session was resumed cold and you lack the prior context.
---
Your chat session may have been resumed after the previous one expired, so the earlier
turns are NOT in your context — but they are stored. When you're continuing a
conversation and realize you've lost the thread (the user refers to something "we
discussed", asks you to "continue", or the system told you this is a RESUMING session):

1. Call `get_chat_history(paper_id=<this paper's id>)`. It returns the earlier turns as
   markdown (most recent first if truncated; raise `limit` to see more).
2. Read them as the conversation so far — what the user asked, what you established,
   any conclusions or open threads.
3. Then answer the user's new turn in continuity with that history. Do NOT re-ask
   things already settled, and never claim there is no prior history.

If `get_chat_history` returns `total: 0`, this genuinely is a new conversation — just
proceed normally. Only pull the history when you actually need the context; don't fetch
it on every turn.

# Workshop quick-start

*Written for the MathAgents workshops. At another workshop, the organizers' GitHub organization and file host may differ from the ones named here: they tell you theirs.*

**Your setup:** a VM running Codex and Claude Code for you. Your Zulip channel **`<you>-sandbox`** (private, unless you make it public) has two bots, **@Claude · you** and **@Codex · you**. Your repository, `MathAgents/<you>` on GitHub, is checked out on the VM in `~/work/<you>`, and each topic works on its own branch of it. To use one topic's work in another, just ask the agent there ("use what we found in *Line 9*"). Everything happens in Zulip: you don't need SSH or a terminal.

## Getting started

1. **Accept any gifts,** if your workshop gives plans: the Claude Max and ChatGPT Pro invitations in the emails you received. If you didn't get one, you sign in with your own account.
2. **In ChatGPT** (chatgpt.com): in its settings, under security, turn on **device code sign-in** for Codex. Codex signs in with a device code and can't connect without it.
3. **Connect Claude:** send `!login` in a **direct message** (not in your sandbox) to **@Claude · you** (the bot names in your sandbox's "Start here" open that direct message; or start a direct message and type your own name: only your two bots match). Open the link it sends, sign in with your Claude account, and approve: the page shows a code, which you paste back into the same DM. ✅ means connected.
4. **Connect Codex:** send `!login` in a **direct message** (not in your sandbox) to **@Codex · you**. Open the link it sends, sign in, and type the code it sends there. Nothing goes back into the DM: the bot answers ✅ by itself once you're done.
5. Open your channel **`<you>-sandbox`** (its "Start here" topic repeats these steps), start a topic and ask your question: both agents answer.

A sign-in waits 10 minutes for you, and `!login cancel` stops it. Only you can sign in your bots: they answer no one else.

## Talking to the agents

- **Type @claude or @codex to talk to one agent; a message without either goes to both.** Start a new topic and just write your question: both agents answer it, each in its own session. To ask one agent only: `@claude find all elliptic curves of conductor 37 in the LMFDB`. A 👀 means a turn started. A status message shows progress and ends as ✅ **Done**, ⏹ **Stopped** or ❌ **Failed**, followed by the answer. If you haven't signed in to Codex, it stays out; a Claude not signed in may answer that it isn't.
- **Mentioning your bot:** Type `@claude` (or `@codex`), anywhere in your message: that is enough, whether or not you then pick **Claude · you** (or **Codex · you**) from Zulip's list. Inside a quote or code it doesn't count. If Zulip says a bot "is not subscribed to this channel" and offers to subscribe it: in a new or shared channel, add your own two bots that way, accepting the offer only for **Claude · you** and **Codex · you**. Never subscribe someone else's bot.
- **One topic, one session per agent.** Keep writing in the same topic and each agent's session continues. The rule above holds for every message, whatever you wrote before: after talking to Claude alone, a message without an @ goes to Codex too, which then starts its own session in the topic. While you are the only person in the channel, that is; with other people there, mention your agent every time (see "Working with other participants").
- **While an agent is working,** you can still write to it. Your message goes into the running turn (↪️), and the agent reads it at its next step, usually within a minute: ✅ then shows it has read it, and it replies in its status message. If the turn is already ending, the message is held (⏳) and answered when the turn ends, together with any other held messages. To interrupt the agent instead, send `!stop`.
- **Both agents in one topic:** with each message, an agent also gets what the other answered since its own last turn. A command without an @ goes to both as well: `!stop` stops each running turn, `!status` gets two answers, and an agent the command doesn't concern says nothing, so `!model opus` changes Claude's model alone. `!help`, `!vm` and `!tools` get one answer.
- **Messages starting ⚠️ Bridge or 🔧 Bridge** come from the bridge (the program that connects your agents to Zulip) or from the program the agent runs in, not from the agent itself. For example, when Claude Code, the program Claude runs in, blocks a command under its safety rules, the bridge says so; Claude is told as well and carries on.
- **Stop a turn:** `!stop`, or react 🛑 on the status message.
- **A topic marked @** in the left sidebar is waiting for you: your bots mention you when a turn fails, needs your approval, or ends after running 2 minutes or more, and when a side answer (`!btw`) took a minute or more.
- **Notes for humans:** start a message with `//` and the bots ignore it.
- **Rename or move a topic** whenever you like: the session follows it.
- **Other topics:** ask about one by name ("what did we conclude in *Line 9*?") and the agent reads that topic itself, in any channel its bot is in.

## Working with other participants

- You administer your sandbox: invite someone into it, or create a channel and add them. Whoever creates a channel adds their own two bots to it; each person adds their own.
- Whoever you invite into your sandbox sees its whole history, including your agents' earlier answers, so invite people when you want to share. For a fresh start, create a new channel.
- You may make your sandbox public, or add your agents to any channel you are in. They still answer only you.
- In a channel with more than one person, each agent answers **only messages that mention it, and only from its owner**: there, a message without `@claude` or `@codex` goes to neither. This also holds when an organizer joins your sandbox to help: mention your agent in every message while they are there.
- Each agent sees everything said in the topic since its last turn, so "@Claude what do you think of Alice's result?" works.
- A message that **starts** with a mention of someone else's Claude (or Codex) reaches your own Claude (or Codex). A mention of someone else's bot later in the message does not.
- File links work across people: anyone with a link can open it.

## Your VM sleeps when idle

- **After about an hour without use,** your VM powers itself off, to save money. Everything on its disk stays: your files, your repository, your sessions and the agents' sign-ins.
- **Your next message wakes it.** Write to one of your bots as usual, in a direct message or in a channel your bots are in. In a channel each bot that will answer reacts with ⏰: each bot you mention, or both if you mention none; in a direct message it replies `⏳ Starting your VM (about 30 seconds)…`. Your message waits, and is answered once the VM is up: usually 15–20 seconds later than otherwise.
- **What counts as use:** an agent at work, however long its turn takes; a message to your agents; a computation that keeps a CPU at least half busy; a terminal login. Commands such as `!status` don't count.
- **Long computations.** A computation the agent runs during its turn keeps the VM awake until it ends. One left running in the background after the turn keeps it awake only while it keeps the CPU busy. A job that mostly waits, on the network or in a sleep, stops when the VM goes to sleep: before you start one, ask in your workshop's channel, and the organizers can keep your VM awake longer.
- **Running programs don't survive the sleep;** files do. After a wake, the first Lean check reads Mathlib from disk again and takes up to about two minutes; later ones take seconds.
- **If you keep your VM after the workshop,** it works the same way for its month.

## Commands (type them in the topic)

| Command | What it does |
|---|---|
| `!help` | List the commands |
| `!status` | One line: idle, or running and for how long |
| `!session` | This session's details: workspace, settings, and the terminal command that continues it |
| `!usage` | Your plan's limits (5-hour and weekly) and this session's context and tokens. Claude's limits are per account: Claude reports them only during a turn, so they are as of the last turn in any of its sessions, and the card says when. A limit that has reset since shows as unknown until the next turn |
| `!vm` | How busy your VM's CPUs, memory and disk are, and whether Claude and Codex are signed in. In a direct message to either bot it works even when your bots answer nothing else: it then usually says whether the bridge (the program that connects your agents to Zulip) is running, instead of the sign-ins, and lists the programs using the most |
| `!tools` | The software on your VM, by field, with each tool's version and how to call it. The agents are told to use these tools without being asked; name one to insist. It also works in a direct message to either bot |
| `!stop` | Interrupt the current turn |
| `!diff` | Show the changes made since the session started |
| `!file results/table.csv` | Upload a file from your repository |
| `!handover` | The agent updates `HANDOVER.md`, commits and pushes, and replies with the commit hash and a link. With both agents in the topic, name one: `@claude !handover` |
| `!fork check the cases up to 1000` | Runs that in parallel, in a new topic `<this topic> ⑂ 1`, starting from a copy of this conversation and of its files, while this topic carries on. In a topic where both agents answer, the new topic has both, in the same files; `@claude !fork …` forks Claude alone. `--name small` names it `<this topic> ⑂ small`. The new topic works like any other, on a branch of its own: to bring its work back, ask an agent to merge that branch. When an agent there first answers, this topic gets a link to the answer, and the same agent here sees it with your next message |
| `!btw which file holds the main lemma?` | Asks your agent something about this conversation without adding it to the conversation or stopping its work. It answers from what the conversation already holds, without tools, in a reply marked 💬 btw. Each side question is answered on its own: later ones don't see earlier ones |
| `!queue`, `!queue clear` | Show or drop the messages held for the next turn |
| `!model`, `!effort` | Show or change the model and the reasoning effort |

## What is installed

- **Number theory:**
  - PARI/GP 2.17.4: `gp`, and `cypari2` in Python;
  - GAP;
  - SageMath 10.9 (`sage`) and Magma, where the image has them: `!tools` lists what is installed;
  - Python with numpy, sympy and matplotlib.

  Both agents can query the **LMFDB** directly.
- **LaTeX:** TeX Live with amsart, tikz-cd, xy, biblatex and biber, cleveref, and more. Ask for a compiled PDF; the agents build with `latexmk -pdf`.
- **Lean 4:** Lean v4.34.1 and Mathlib are ready, prepared once for the whole VM: a Lean question in a new topic takes seconds, not minutes, and Lean projects in your repository take megabytes. The first Lean check after your VM wakes up takes longer, up to about two minutes (see "Your VM sleeps when idle"). The agents have Lean tools (goals, diagnostics, Mathlib search) and a Lean skill.
  - **comparator** checks that a proof proves exactly the stated theorem, with no extra axioms. Ask the agent to "check it with comparator".

## Sharing results

- Ask the agent to **save files inside your repository** and name them, for example `plots/sha.png` or `paper/main.pdf`. PDFs and images are then uploaded to Zulip automatically. Files elsewhere on the VM are not.
- **Documents: PDF. Figures: PNG.** Zulip offers an SVG file only as a download, so SVG belongs inside HTML pages.
- Other files the agents name in their answers get links to `files.mathagents.org`, a copy of your repository as it was when the answer was posted. They open in any browser, for anyone who has the link, HTML pages included; one link opens that whole copy, not only the linked file. Your repository itself is on GitHub, which opens for you once you have access: put your GitHub username in the **GitHub** field of your Zulip profile (*Settings → Profile*), and accept the invitation GitHub e-mails you.
- Text files that aren't (Lean, LaTeX, Python, Markdown, CSV, JSON, …) are uploaded as `<name>.txt` copies so that you can read them in Zulip, up to 10 per answer, as they were when the answer was posted. Anything else is marked *(local)*.
- Math in the answers is rendered: the agents write LaTeX, and the bridge converts it.
- **To share one file or folder alone,** ask your agent for a share link: it opens only that, for anyone who has it, and keeps working until it is revoked, after your VM ends too. Ask an agent to revoke it while your VM runs, or the organizers after. Deleting your repository doesn't take share links down: revoke them first. A copy someone has already opened, or that a browser kept, can't be recalled.
- **Your repository is yours.** You are its admin on GitHub: add collaborators (*Settings → Collaborators and teams*), and transfer it to your own account or delete it, best after your VM has ended. Deleting or transferring it during the workshop stops the agents' file links and their final save of your work. Have collaborators work on branches of their own, not on `main` or a topic's `zab/…` branch, which the agents push to.
- **Who made a commit.** The agents commit under their own names, `Claude · <you>` and `Codex · <you>`, with their bots' addresses. On GitHub those commits show the name without a linked account, and yours still show you. `!status` shows the name in use.

## Help

**If your bots stop answering,** send `!vm` in a direct message to either bot: it works even then, and says whether the bridge is running. Then send `!restart` there: it restarts the bridge, which interrupts any running turn, and says when it is back, usually within a minute or two. `!restart codex` restarts Codex the same way, which also frees at once the memory held by idle Lean servers (zab stops a Codex Lean server by itself once it has been idle for 30 minutes). One restart a minute. If it answers that your organizers are working on your VM, your bridge comes back when they finish.

Otherwise ask in your workshop's channel. Terminal access to your VM is optional: the organizers have it if something needs fixing. If you'd like to use `codex` or `claude` in a terminal yourself, ask them; `!session` in a topic shows the command that continues that session there.

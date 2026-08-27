# CLI
> Every command, grouped by what you are trying to do.

## Setting up

    dispatch upgrade                    what an older board is missing
      --apply                           write those changes
    dispatch resume                     clear a pause (including the expansion alarm)

    dispatch init [path]                scaffold .dispatch/ in a repo
      --test-cmd "pytest -q"            what tests_pass runs
      --lint-cmd / --build-cmd          what lint_clean / build_ok run
      --auth subscription|api_key       which credentials agents use
      --no-verify                       store the test command without running it
      --sandbox                         require confinement (refuse without it)
      --no-sandbox                      run agents unconfined
      --sandbox-backend auto|srt        auto keeps the internet open
      --git-init                        create the repo if there isn't one
      --force                           overwrite config, workflows, prompts

## Putting work on the board

    dispatch add TITLE
      --brief "..."                     the literal prompt the agent receives
      --accept "..."                    acceptance criterion (repeatable)
      --scope "src/**"                  glob the work is confined to (repeatable)
      --type development                card type -> pipeline
      --parent t_abc123                 containment and a shared budget
      --depends-on t_abc123             ordering
      --tag api                         repeatable
      --priority 70                     higher goes first (default 50)
      --budget 40                       usd ceiling for this subtree
      --max-attempts 3                  returns before dead-lettering
      --start                           straight onto stage 1

    dispatch edit ID                    --title --brief --append-brief --accept
                                        --add --scope --tag --type --priority
                                        --max-attempts --requeue
    dispatch start ID...                move backlog cards onto stage 1
    dispatch link SRC DST               --kind finish_to_start | artifact | mutex
    dispatch unlink SRC DST             remove an edge   --kind ...
    dispatch edges [ID]                 show the edges on the board
    dispatch cancel ID...               cancel a card
      --cascade                         also cancel everything beneath it
      --only                            cancel just this card

## Direction and memory

    dispatch intent "..."               describe what you want; an agent plans it
      --title / --priority              ("-" reads the description from stdin)
    dispatch plan ID                    read the proposed plan   --json

    dispatch memory                     everything agents have learned
    dispatch memory search "..."        ranked   --tags --limit
    dispatch memory add TITLE --body "..." --tags a,b --kind pointer
    dispatch memory show ID / rm ID

## Looking at the board

    dispatch ls                         --all --stage build --type bugfix --json
    dispatch show ID                    brief, criteria, blockers, gates, runs
      --full                            do not clip the brief or the evidence
    dispatch status                     one screen: scheduler, flight, spend
    dispatch blocked                    for each card, exactly what holds it
    dispatch log --limit 200            the append-only event log
    dispatch proposals                  what was proposed and how it was decided

## Running it

    dispatch up                         scheduler + web board (foreground)
      -d                                detach
      --no-web                          scheduler only
      --port 7777                       default: a stable port per repo
      --host tailscale                  local | tailscale | any | an address
    dispatch down                       stop a detached scheduler
    dispatch serve                      web board only  --port --host
    dispatch tick -n 3 --wait 120       run the loop in the foreground and watch
    dispatch gc                         remove worktrees for finished cards

## Waiting, from a session

    dispatch wait [ID...]               block until cards land
      --tag api / --type bugfix         choose what to wait on
      --timeout 900                     seconds; 0 waits indefinitely
      --through-checkpoints             don't return when a card needs you
      --json / --quiet
      exit: 0 landed · 1 failed · 2 timed out · 3 needs a human

    dispatch attend                     block until a decision is the session's
      --timeout 480                     seconds before returning "still working"
      --audience session|human          whose decisions to wait for
      --json
      exit: 3 decide it · 2 still working · 0 idle · 4 relay to a person · 1 failed

    dispatch respond ID approve|amend|reject --as session

    dispatch channel                    run as a Claude Code channel (pushes in)
      --install                         register it in .mcp.json
      --poll 2.0                        seconds between event-log checks

    dispatch hook stop                  Claude Code Stop hook: report the board
      --block-while-busy                hold the session open until it settles
      --max-blocks 20                   let go after this many turns

## Decisions

    dispatch needs                      open checkpoints, with their context
    dispatch respond ID approve
    dispatch respond ID amend  --note "..."
    dispatch respond ID reject --note "..."

## For agents, from inside a worktree

    dispatch propose --from $DISPATCH_TASK_ID --kind add_task \
      --title "..." --brief "..." --rationale "..."

    kinds: add_task split add_dep amend_brief raise_blocker
           request_gate cancel escalate
    --accept is REQUIRED for add_task and split — a card with nothing to
    check is refused before it runs, so proposing one only spends attention

    also:  --accept --scope --task --src --dst --gate --reason --append --json
           --confidence 0.8 --urgency low|normal|high

## Pipelines

    dispatch workflows                  show every pipeline, with problems
    dispatch workflows export           write .dispatch/workflows.json
    dispatch workflows import --file P  load one (defaults to .dispatch/)

## These docs

    dispatch docs                       list topics
    dispatch docs cards                 read one
    dispatch docs cards --page 2        page through a long one
    dispatch docs --search "quota"      find the topic that mentions it
    dispatch docs --all                 everything, for piping
    dispatch docs --export docs/        write it out as browsable files

## Anywhere

    --root PATH                         operate on a board elsewhere
    --version

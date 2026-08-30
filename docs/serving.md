# Serving
> Ports for several boards at once, and viewing one from another device.

## Ports

`server.port` defaults to `"auto"`: a stable port derived from the repo's path,
in the range 7777–7876. The same checkout always gets the same number, so
bookmarks keep working and two boards never fight over a default.

    dispatch init            # prints the port this repo will use
    dispatch status          # prints the board URL, live or not
    dispatch up --port 8080  # override for one run

Set a fixed number in `config.json` if you prefer. If the chosen port is taken,
the board walks forward up to 12 ports and logs where it landed.

## Where it binds

`server.host`, or `--host`:

| value | binds to | who can reach it |
|---|---|---|
| `local` (default) | `127.0.0.1` | this machine only |
| `tailscale` | your tailnet address | your tailnet |
| `any` | `0.0.0.0` | anything that can route to this machine |
| an address | that address | depends |

## Over Tailscale

    dispatch up -d --host tailscale

    board: http://your-box.tailXXXX.ts.net:7837
    board: http://100.x.y.z:7837

It binds to the tailnet address **specifically**, not `0.0.0.0` — so the board
is reachable from your other devices and not from the local network. Loopback
will refuse the connection, which is the expected result, not a fault.

The board has no login of its own. **Your tailnet ACLs are the access control.**
Keep it off `any` unless you know what is on that network.

For HTTPS and a name that works without the port, put Tailscale in front of a
loopback board instead:

    dispatch up -d                              # loopback, on its auto port
    tailscale serve --bg --https=443 127.0.0.1:7837

## Several boards at once

Nothing is shared between boards — each is a `.dispatch/` inside its own repo,
with its own scheduler process, port, and database.

    cd ~/code/api  && dispatch up -d      # :7837
    cd ~/code/web  && dispatch up -d      # :7803
    cd ~/code/api  && dispatch status     # each reports its own URL

`dispatch down` stops the one for the repo you are standing in.


## Filtering the board

A board that has been running for days fills with landed and abandoned cards.
The **Done** and **Cancelled** toggles hide them, and the search box narrows by
title, tag or id. The choice is remembered per browser.

Hiding is never silent: the bar says how many cards are out of view, each
column head reads `shown/total`, and a column emptied by a filter says so
rather than reading `empty`. A filtered board that looks like an unfiltered one
is how you conclude a card was never created.


Next: `dispatch docs billing`

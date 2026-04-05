# FishClient

**FishClient** is a Python client for interacting with [fishtank.live](https://www.fishtank.live/)

---


## Installation

```bash
pip install fishclient

or 

pip install git+https://github.com/pluhian/fishclient.git
```

Or clone manually:

```bash
git clone https://github.com/pluhian/fishclient.git
cd fishclient
pip install .
```

---

## Usage

```python
from fishclient import FishClient

client = FishClient(cookie="your_auth_cookie_here")

@client.dispatcher.on("connect")
def handle_connect():
    print("Connected!")

@client.dispatcher.on("chat:message")
def handle_chat(msg):
    print("New chat message:", msg)

@client.dispatcher.on("disconnect")
def handle_disconnect():
    print("Disconnected from server.")

client.connect()
```

---

## Event Handling

Events are dispatched using the `dispatcher.on(event_name)` decorator.

Supported examples:

```python
@client.dispatcher.on("chat:message")
def on_chat(msg): ...

@client.dispatcher.on("profile:updates")
def on_profile(data): ...

@client.dispatcher.on("disconnect")
def on_disconnect(): ...
```


## Known WebSocket Event Types

```
connect
initial-data
disconnect
connect_error
presence
force-refresh
panel:change
chat:room
chat:mute
chat:message
chat:direct
chat:direct:read
chat:direct:remove
chat:message:remove
chat:messages
chat:messages:remove
happening
tts:queued
tts:update
tts:price
tts:url
fishtoy:queued
fishtoy:update
sfx:price
sfx:insert
sfx:update
sfx:queued
sfx:url
sfx-option:new
sfx-option:update
sfx-option:remove
item:new
item:update
items:update
item-details:new
item-details:update
item-details:remove
clan-alliance:new
clan-alliance:remove
clan-alliance-proposal:new
clan-alliance-proposal:remove
clan:new
clan:remove
clan:update
clan-rank
clan-tokens:update
clan-tokens:set
clan-invite
clan-kick
clan:debuffs:insert
clan:debuffs:set
profile:get
profile:updates
tokens:update
tokens:set
challenges:completed
daily-challenges:updated
global-challenge:start
global-challenge:stop
global-challenge:status
global-challenge:event
feature-toggles:get
feature-toggles:update
xp:update:all
xp:update
xp:set
xp:daily:claim
trade:request
trade:open
trade:close
trade:update
trade:offer
trade:accept
trade:complete
trade:cancel
trade:ping
announcement
contestants:new
contestants:update
contestants:remove
contestants:endorsements
notification:global
notifications:new
notifications:get
live-stream:set
live-stream:status
live-stream:new
live-stream:update
live-stream:remove
live-stream:balance
clip:created
poll:start
poll:stop
poll:vote
zones:update
zones:claim
zones:claims:delete
quest:new
quest:remove
quest:update
quest:tokens
goal:new
goal:remove
goal:update
goal:tokens
total-viewers:update
fish:tokens
fish:toys
fish:toys:buy
fish:toys:approve
fish:toys:reject
misc:play-sound
audition-toy
audition-effect
refund:update
refund:new
stock:prices
stock:holdings
stock:ipo-purchased
stock:update
stock:new
stock:remove
```

---

## License

MIT © Rayan

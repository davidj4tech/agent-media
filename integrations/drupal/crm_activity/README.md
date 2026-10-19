# CRM Activity

A per-contact timeline for Drupal's [CRM](https://www.drupal.org/project/crm)
module. Each entry is one contact's activity on one channel on one day: "texted
12 times", "2 calls, 14 min", or a note. Entries are rollups that an agent
writes over JSON:API; message bodies are never stored.

agent-media writes them (`agent_media_server/timeline.py`), and its server
answers `GET /contacts/<id>/timeline` from them. Any writer can use the
module, though: it is a plain content entity.

## What it adds

- **`crm_activity`**, a content entity with these fields:
  - `contact` → `crm_contact` (required)
  - `channel`: `sms`, `signal`, `messenger`, `instagram`, `teams`, `whatsapp`, `call`, `email`, `chat` (old Google Talk and gateway chats) or `note`
  - `day`: a date
  - `count_in`, `count_out`, `duration` (seconds on calls)
  - `summary`
  - `source_key`: unique. A writer upserts by it, so re-running a rollup
    updates the entry instead of adding a second one.
  - `created`, `changed`
- **An Activity tab** on every contact (`/crm/contact/{id}/activity`), and
  the full list at `/crm/activity` (in the CRM menu).
- **Permissions**: `view`, `create`, `edit`, `delete` and `administer crm
  activity`. Viewing an entry also needs view access to its contact.
- **JSON:API** at `/jsonapi/crm_activity/crm_activity`, like any content
  entity, when `jsonapi` is on and not read-only.
- Deleting a contact deletes their activity.

## Install

Drupal 11.1 or later, with `crm` installed.

```
cp -r crm_activity web/modules/custom/
drush en crm_activity
drush config:export        # if the site keeps config in git
```

With Composer, add this directory as a `path` repository and require
`agent-media/crm_activity`.

## The writer's account (OAuth)

The writer signs in as a machine, using [Simple OAuth](https://www.drupal.org/project/simple_oauth)'s
`client_credentials` grant, with a role that holds only what it needs:

1. **A role**, e.g. `agent_media_contacts`, with these permissions:
   - `view any person crm contact`
   - `create person crm contact`
   - `edit any person crm contact`
   - `view crm activity`, `create crm activity`, `edit crm activity`
2. **A user** holding only that role. client_credentials tokens act as it.
3. **A scope** (Simple OAuth 6: *Configuration → Simple OAuth → Scopes*)
   with *Client credentials* on and granularity *Role*, set to that role.
4. **A consumer (client)**:
   - grant type *Client credentials*
   - the scope above
   - the user above as its *default user*
   - confidential, with a secret

   Hand its id and secret to the writer: never into git.

The writer then asks `POST /oauth/token` for
`grant_type=client_credentials&client_id=…&client_secret=…&scope=…`, and
sends `Authorization: Bearer <token>` to `/jsonapi/…`.

For agent-media, put these in `~/.config/agent-media/drupal-crm.json` (0600)
on the server host:

```json
{"url": "https://crm.example.com", "client_id": "…", "client_secret": "…",
 "scope": "agent_media_contacts"}
```

## Anonymous JSON:API

JSON:API lists the entries an anonymous visitor may not see in
`meta.omitted`, along with their ids. Entry contents stay hidden, but their
count and UUIDs do not. If that matters, refuse `/jsonapi` to requests
without an `Authorization` header, either at the proxy or with a route
subscriber.

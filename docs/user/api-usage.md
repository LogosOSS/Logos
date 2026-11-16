---
title: Using the API
---

# Using the Logos API

Logos implements the OpenAI chat completions, responses, embeddings, audio,
files, and batch APIs. Replace the host and the model name in the examples with the
values from your administrator.

Keep API keys in environment variables or in a secret manager. Do not commit
API keys to source control. Do not put API keys in client-side applications.

## Streaming

To receive server-sent events, set `stream` to `true`:

```bash
curl https://logos.aet.cit.tum.de/v1/chat/completions \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{"model":"your-model","stream":true,"messages":[{"role":"user","content":"Explain recursion."}]}'
```

## Reusing answers to similar questions

Logos can answer a chat-completions request with a stored answer to a similar
question. Then Logos does not send the request to a model. To use this
function, send the `logos-context` header. Its value is the name of the context
of your questions, for example a shop or a course:

```bash
curl https://logos.aet.cit.tum.de/v1/chat/completions \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -H "logos-context: shop-a" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"Where is my package?"}]}'
```

An administrator loads the stored answers for each context (see
[Operations and Troubleshooting](../admin/operations.md#load-answers-for-answer-reuse)).
Logos sends a stored answer only if its question is very similar to your
question. The most similar stored questions must also have the same intent.
Requests share answers only if they have the same context. Their API keys must
be in the same team, or they must use the same API key if it is not in a team.

Logos uses stored answers only for a request with these properties:

- The request does not use streaming, and `n` is 1 or not set.
- The request has one user message and no other messages. A system message, a
  developer message or an earlier turn is not permitted.
- The user message has only the fields `role` and `content`, and `content` is
  text.
- The request sets no fields other than `model`, `messages`, `stream`, `n`,
  `temperature`, `top_p`, `seed`, `user`, `metadata`, `store` and `timeout_s`.

Fields such as `max_tokens`, `stop`, `tools` or `response_format` change the
correct reply, and a stored answer cannot obey them. Logos sends other
requests, and requests without the header, to a model as usual. If a request
names a model, your API key must have access to that model. If it does not,
Logos rejects the request as usual.

A reply with a stored answer has `"model": "logos-answer-cache"`, zero `usage`
and the `X-Logos-Cache: hit` response header. The `logos.answer_reuse` object
in the reply identifies the stored answer and the model that wrote it.

For the complete request and response schema, see the Swagger UI at `/docs`.

## Conversation State in the Responses API

Logos does not keep conversation state at the provider. Requests that set
`previous_response_id` or `conversation`, or that have an `input` item of
type `item_reference`, are rejected with status `400`. Batch input files that
contain such requests are rejected too. Send the whole conversation in `input`
instead.

This is a limitation of Logos. Logos reaches each provider with one account
for all teams, so the provider cannot tell which team stored a response, and
a reference could load the conversation of another team.

## Image Generation in the Responses API

A Responses request can let the model call the built-in image tool:

```bash
curl https://logos.aet.cit.tum.de/v1/responses \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-luna","input":"Draw a lighthouse at sunset.",
       "tools":[{"type":"image_generation","model":"gpt-image-1.5","quality":"medium"}],
       "tool_choice":{"type":"image_generation"}}'
```

The image tool runs a second model, so the key needs access to **both** models:
the model in `model` and the image model in the tool entry (`gpt-image-1` when
the entry names none). Logos refuses a request with `403` when the key may not
use the image model. A request may use one image model.

On Azure, the image model is a deployment of its own on the same resource as the
chat model. Logos names that deployment in the header that Azure requires. If the
resource has no deployment of the image model, Azure refuses the request. The
image is returned base64-encoded in the `image_generation_call` output item.

## Audio Transcription and Translation

Logos implements the OpenAI multipart audio APIs:

- `POST /v1/audio/transcriptions` writes the speech in the original language.
- `POST /v1/audio/translations` writes the speech in English.

```bash
curl https://logos.aet.cit.tum.de/v1/audio/transcriptions \
  -H "Authorization: Bearer $LOGOS_API_KEY" \
  -F "file=@./speech.wav" \
  -F "model=whisper-1" \
  -F "response_format=verbose_json" \
  -F "timestamp_granularities[]=word"
```

Your API key must have access to the model. Logos sends the original
multipart fields and the file metadata to the provider. The provider can be a
cloud provider, for example an Azure OpenAI Whisper deployment. It can also be
a compatible backend on a worker node. Logos returns the status and the
content type of the provider. The response formats are JSON, verbose JSON,
plain text, SRT, and VTT.

Logos does not write the audio content or the credentials to the usage log or
to the async job records. It records only the file name, the media type, and
the file size.

| Limit | Default | Setting |
| --- | --- | --- |
| Audio file (1 file in each request) | 25 MiB | `LOGOS_MAX_AUDIO_UPLOAD_BYTES` |
| Each text form field | 64 KiB | `LOGOS_MAX_AUDIO_FORM_FIELD_BYTES` |
| Total multipart request | 30 MiB | `LOGOS_MAX_AUDIO_REQUEST_BYTES` |

The default file limit is the limit of the upstream Whisper API. Logos checks
the total request limit before it writes the file to a temporary file.

If the provider reports the usage as a duration, Logos stores the duration in
milliseconds. It then applies the price for each second from the provider
catalogue. Thus Logos does not discard parts of a second.

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

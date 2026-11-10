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
earlier question, without calling a model. To opt in, name the context your
questions belong to, such as a shop or a course, in the `logos-context` header:

```bash
curl https://logos.aet.cit.tum.de/v1/chat/completions \
  -H "Authorization: ******" \
  -H "Content-Type: application/json" \
  -H "logos-context: shop-a" \
  -d '{"model":"your-model","messages":[{"role":"user","content":"Where is my package?"}]}'
```

A stored answer is reused only when its question is very similar to yours and
the most similar stored questions agree on what was asked. Answers are shared
only between requests with the same context from API keys of the same team
(or from the same key, if it belongs to no team).

Reuse applies to non-streaming requests that consist of a single user message
(no system or developer message, no earlier turns) and use no tools or
response format. Other requests, and requests without the header, are handled
as before. A request that names a model is only answered from stored answers
if your API key may use that model; otherwise it is rejected as usual.

A reused answer reports `"model": "logos-answer-cache"`, zero `usage`, and the
`X-Logos-Cache: hit` response header. The `logos.answer_reuse` object in the
response names the stored answer and the model that originally wrote it.

For the complete request and response schema, see the Swagger UI at `/docs`.

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

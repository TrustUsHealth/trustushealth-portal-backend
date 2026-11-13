const SECRET_PROPERTY = "GOOGLE_SCRIPT_SECRET";
const IDEMPOTENCY_PREFIX = "MAIL_IDEMPOTENCY_";
const IDEMPOTENCY_RETENTION_MS = 24 * 60 * 60 * 1000;

function doPost(event) {
  let payload;
  try {
    payload = JSON.parse(event.postData.contents);
  } catch (_error) {
    return jsonResponse({ ok: false, error: "invalid_request" });
  }

  if (!payload || typeof payload !== "object" || typeof payload.secret !== "string") {
    return jsonResponse({ ok: false, error: "invalid_request" });
  }
  const expectedSecret = PropertiesService.getScriptProperties().getProperty(SECRET_PROPERTY);
  if (!expectedSecret || payload.secret !== expectedSecret) {
    return jsonResponse({ ok: false, error: "unauthorized" });
  }
  if (!isValidPayload(payload)) {
    return jsonResponse({ ok: false, error: "invalid_request" });
  }

  const properties = PropertiesService.getScriptProperties();
  const key = IDEMPOTENCY_PREFIX + hashKey(payload.idempotencyKey);
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) {
    return jsonResponse({ ok: false, error: "busy", idempotencyKey: payload.idempotencyKey });
  }

  try {
    removeExpiredKeys(properties);
    const previous = properties.getProperty(key);
    if (previous) {
      if (previous.indexOf("sent:") === 0) {
        return jsonResponse({ ok: true, duplicate: true, idempotencyKey: payload.idempotencyKey });
      }
      return jsonResponse({ ok: false, error: "delivery_uncertain", idempotencyKey: payload.idempotencyKey });
    }

    // Store before sending so a retry after a lost response cannot send the same message twice.
    properties.setProperty(key, "processing:" + Date.now());
    const inlineImages = {};
    Object.keys(payload.inlineImages).forEach(function (contentId) {
      const image = payload.inlineImages[contentId];
      inlineImages[contentId] = Utilities.newBlob(
        Utilities.base64Decode(image.content),
        image.contentType
      );
    });
    const attachments = payload.attachments.map(function (attachment) {
      return Utilities.newBlob(
        Utilities.base64Decode(attachment.content),
        attachment.contentType,
        attachment.filename
      );
    });

    MailApp.sendEmail({
      to: payload.to,
      subject: payload.subject,
      body: payload.text,
      htmlBody: payload.html,
      inlineImages: inlineImages,
      attachments: attachments
    });
    properties.setProperty(key, "sent:" + Date.now());
    return jsonResponse({ ok: true, duplicate: false, idempotencyKey: payload.idempotencyKey });
  } catch (_error) {
    properties.setProperty(key, "uncertain:" + Date.now());
    return jsonResponse({ ok: false, error: "delivery_failed", idempotencyKey: payload.idempotencyKey });
  } finally {
    lock.releaseLock();
  }
}

function isValidPayload(payload) {
  return payload
    && typeof payload.idempotencyKey === "string"
    && /^[A-Za-z0-9_-]{20,128}$/.test(payload.idempotencyKey)
    && typeof payload.to === "string"
    && payload.to.length > 0
    && typeof payload.subject === "string"
    && typeof payload.text === "string"
    && typeof payload.html === "string"
    && payload.inlineImages
    && typeof payload.inlineImages === "object"
    && !Array.isArray(payload.inlineImages)
    && Object.keys(payload.inlineImages).every(function (contentId) {
      const image = payload.inlineImages[contentId];
      return contentId.length > 0
        && image
        && typeof image.contentType === "string"
        && image.contentType.indexOf("/") > 0
        && typeof image.content === "string";
    })
    && Array.isArray(payload.attachments)
    && payload.attachments.every(function (attachment) {
      return attachment
        && typeof attachment.filename === "string"
        && attachment.filename.length > 0
        && typeof attachment.contentType === "string"
        && attachment.contentType.indexOf("/") > 0
        && typeof attachment.content === "string";
    });
}

function hashKey(value) {
  return Utilities.base64EncodeWebSafe(
    Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, value, Utilities.Charset.UTF_8)
  ).replace(/=+$/, "");
}

function removeExpiredKeys(properties) {
  const cutoff = Date.now() - IDEMPOTENCY_RETENTION_MS;
  const all = properties.getProperties();
  Object.keys(all).forEach(function (key) {
    if (key.indexOf(IDEMPOTENCY_PREFIX) === 0) {
      const recordedAt = Number(all[key].split(":").pop());
      if (recordedAt < cutoff) {
        properties.deleteProperty(key);
      }
    }
  });
}

function jsonResponse(body) {
  return ContentService.createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}

// Use the real client SDK against either local server. Never print auth tokens.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const { default: PocketBase } = await import(pathToFileURL(process.env.C10_SDK_PATH));
const pb = new PocketBase(process.env.C10_URL);
const viewer = new PocketBase(process.env.C10_URL);
const name = process.env.C10_IDENTITY;
const proof = {};
let customer;
let unsubscribe;
try {
  const auth = await pb.collection("users").authWithPassword(name, "abcde123");
  assert.equal(auth.record.username, name);
  const refreshed = await pb.collection("users").authRefresh();
  assert.equal(refreshed.record.id, auth.record.id);
  proof.authWithPassword = proof.authRefresh = true;
  await viewer.collection("users").authWithPassword(name + "viewer", "abcde123");
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/YsAAAAASUVORK5CYII=", "base64");
  customer = await pb.collection("buma_customer").create({
    firstname: name, lastname: "SDK", kind: "CUSTOMER_SIMPLE", status: true,
    author: auth.record.id, photo: new File([png], "pixel.png", { type: "image/png" }),
  });
  assert.equal(customer.firstname, name);
  const filtered = await pb.collection("buma_customer").getList(1, 10, {
    filter: pb.filter("firstname = {:name}", { name }), expand: "author",
  });
  assert.equal(filtered.totalItems, 1);
  assert.equal(filtered.items[0].id, customer.id);
  assert.equal(filtered.items[0].expand.author.id, auth.record.id);
  proof.create = proof.filter = proof.expand = true;
  const url = pb.files.getURL(customer, customer.photo);
  const download = await fetch(url);
  assert.equal(download.status, 200);
  assert.deepEqual(Buffer.from(await download.arrayBuffer()), png);
  proof.fileUploadAndDownload = true;
  let receive;
  const event = new Promise(resolve => { receive = resolve; });
  const seen = [];
  unsubscribe = await viewer.collection("buma_customer").subscribe(customer.id, data => {
    seen.push(data);
    if (data.action === "update") receive(data);
  });
  const update = await pb.collection("buma_customer").update(customer.id, { lastname: "Updated" });
  assert.equal(update.lastname, "Updated");
  const timer = setTimeout(() => receive({ timeout: true }), 8000);
  const message = await event;
  clearTimeout(timer);
  assert.equal(message.action, "update", "realtime update event missing");
  assert.equal(message.record.id, customer.id);
  assert.equal(message.record.lastname, "Updated");
  proof.update = proof.realtime = true;
  await pb.collection("roles").update(process.env.C10_VIEWER_ROLE, {
    permission: { customer: { read: false } },
  });
  const beforeDenied = seen.length;
  await pb.collection("buma_customer").update(customer.id, { lastname: "Denied" });
  await new Promise(resolve => setTimeout(resolve, 600));
  assert.equal(seen.length, beforeDenied, "permission revocation must suppress realtime");
  await pb.collection("roles").update(process.env.C10_VIEWER_ROLE, {
    permission: { customer: { read: true } },
  });
  const restored = new Promise(resolve => { receive = resolve; });
  await pb.collection("buma_customer").update(customer.id, { lastname: "Restored" });
  const restoreTimer = setTimeout(() => receive({ timeout: true }), 8000);
  const restoredEvent = await restored;
  clearTimeout(restoreTimer);
  assert.equal(restoredEvent.record?.lastname, "Restored");
  proof.realtimePermissionRevocationAndRestore = true;
  await unsubscribe();
  unsubscribe = null;
  assert.equal(await pb.collection("buma_customer").delete(customer.id), true);
  proof.delete = true;
  customer = null;
  console.log(JSON.stringify({ status: "passed", proof }));
} catch (error) {
  console.log(JSON.stringify({ status: "failed", proof, error: error.message,
    response: error.response ?? null, httpStatus: error.status ?? null }));
  process.exitCode = 1;
} finally {
  if (unsubscribe) await unsubscribe();
  if (customer) await pb.collection("buma_customer").delete(customer.id);
  pb.authStore.clear();
  viewer.authStore.clear();
}

import io
import json
import logging
import os
import sys
import time
import types
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

# Offline: boto3 and botocore are replaced before the handler creates its clients
fake_boto3 = types.ModuleType("boto3")
fake_boto3.client = mock.MagicMock()
fake_botocore = types.ModuleType("botocore")
fake_botocore_config = types.ModuleType("botocore.config")
fake_botocore_config.Config = mock.MagicMock()
sys.modules.update({"boto3": fake_boto3, "botocore": fake_botocore, "botocore.config": fake_botocore_config})

os.environ.update({
    "TELEGRAM_TOKEN": "tg-token",
    "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
    "ALLOWED_CHAT_IDS": "42",
    "GH_TOKEN": "gh-token",
    "GH_REPO": "owner/repo",
    "AWS_REGION": "eu-north-1",
    "S3_BUCKET": "bucket",
    "S3_PROFILE_PREFIX": "profiles/",
    "BEDROCK_MODEL_ID": "eu.amazon.nova-micro-v1:0",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import handler  # noqa: E402

logging.disable(logging.CRITICAL)

CHAT_ID = 42
DISPATCH_URL = handler.GH_WORKFLOW_URL + "/dispatches"


def text_block(text):
    return {"text": text}


def tool_block(name, **args):
    return {"toolUse": {"toolUseId": "tooluse_1", "name": name, "input": args}}


def converse_response(*blocks):
    return {"output": {"message": {"role": "assistant", "content": list(blocks)}}, "stopReason": "end_turn"}


def filtered_response():
    response = converse_response(text_block(" - The generated text has been blocked by our content filters."))
    response["stopReason"] = "content_filtered"
    return response


class FakeHttp:
    """Records every HTTP call and answers GitHub requests with canned responses."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, url, data=None, headers=None, method=None):
        self.calls.append({"url": url, "data": data, "headers": headers or {}, "method": method})
        for prefix, response in self.responses.items():
            if url.startswith(prefix):
                return response
        return 204 if url == DISPATCH_URL else 200, None

    def telegram(self, method):
        return [
            dict(urllib.parse.parse_qsl(c["data"].decode()))
            for c in self.calls
            if c["url"] == f"{handler.TELEGRAM_URL}/{method}"
        ]

    def texts(self):
        return [m["text"] for m in self.telegram("sendMessage")]

    def dispatches(self):
        return [json.loads(c["data"])["inputs"] for c in self.calls if c["url"] == DISPATCH_URL]


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        self.converse = mock.MagicMock()
        self.send_profiles = mock.MagicMock()
        patches = [
            mock.patch.object(handler, "http_request", self.http),
            mock.patch.object(handler.BEDROCK_CLIENT, "converse", self.converse),
            mock.patch.object(handler, "send_profiles", self.send_profiles),
            mock.patch.object(handler, "BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def message(self, text, chat_id=CHAT_ID):
        body = {"message": {"chat": {"id": chat_id, "first_name": "Eugeny"}, "text": text}}
        return self.call(body)

    def callback(self, data, age=0, chat_id=CHAT_ID):
        msg = {"message_id": 7, "date": int(time.time() - age), "chat": {"id": chat_id}, "text": "Deploy?"}
        return self.call({"callback_query": {"id": "cb1", "data": data, "message": msg}})

    def call(self, body, secret="webhook-secret"):
        event = {"headers": {"x-telegram-bot-api-secret-token": secret}, "body": json.dumps(body)}
        return handler.handler(event, None)

    def markups(self):
        return [json.loads(m["reply_markup"]) for m in self.http.telegram("sendMessage")]

    def keyboard(self):
        inline = [m for m in self.markups() if "inline_keyboard" in m]
        self.assertEqual(len(inline), 1)
        return [button["callback_data"] for button in inline[0]["inline_keyboard"][0]]

    def button_labels(self):
        inline = [m for m in self.markups() if "inline_keyboard" in m]
        return [button["text"] for button in inline[0]["inline_keyboard"][0]]


class RoutingTest(HandlerTestCase):
    def test_deploy_asks_for_confirmation_and_does_not_dispatch(self):
        self.converse.return_value = converse_response(
            text_block("<thinking>The user wants OpenVPN in Yandex.</thinking>"),
            tool_block("deploy", cloud="yandex", protocol="openvpn"),
        )
        self.message("подними впн в яндексе, чтобы работал через tcp")

        self.assertEqual(self.keyboard(), ["run:apply:yandex:openvpn", "cancel"])
        self.assertEqual(self.http.dispatches(), [])
        self.assertNotIn("thinking", " ".join(self.http.texts()))

    def test_destroy_defaults_to_aws(self):
        self.converse.return_value = converse_response(tool_block("destroy"))
        self.message("tear it down")

        self.assertEqual(self.keyboard(), ["run:destroy:aws:wireguard", "cancel"])
        self.assertEqual(self.button_labels(), ["✅ Yes, destroy", "❌ Cancel"])
        self.assertEqual(self.http.dispatches(), [])

    def test_invalid_tool_arguments_fall_back_to_defaults(self):
        self.converse.return_value = converse_response(tool_block("deploy", cloud="gcp", protocol="ipsec"))
        self.message("deploy somewhere")

        self.assertEqual(self.keyboard(), ["run:apply:aws:wireguard", "cancel"])

    def test_profile_tools(self):
        cases = {
            "profiles": mock.call(CHAT_ID),
            "profile": mock.call(CHAT_ID, qr_codes=False),
            "qr": mock.call(CHAT_ID, links=False),
        }
        for tool, expected in cases.items():
            with self.subTest(tool=tool):
                self.send_profiles.reset_mock()
                self.converse.return_value = converse_response(tool_block(tool))
                self.message(f"give me the {tool}")
                self.assertEqual(self.send_profiles.call_args_list, [expected])

    def test_status_reports_the_last_run(self):
        run = {"display_title": "apply wireguard in aws", "status": "completed", "conclusion": "success"}
        self.http.responses[handler.GH_WORKFLOW_URL + "/runs"] = (200, {"workflow_runs": [run]})
        self.converse.return_value = converse_response(tool_block("status"))
        self.message("how is the deploy going?")

        self.assertEqual(self.http.texts(), ["Last job: apply wireguard in aws\nStatus: completed\nConclusion: success"])

    def test_small_talk_is_answered_by_the_model(self):
        self.converse.return_value = converse_response(text_block("Hi Eugeny! I can deploy a VPN for you."))
        self.message("hey there")

        self.assertEqual(self.http.texts(), ["Hi Eugeny! I can deploy a VPN for you."])
        request = self.converse.call_args.kwargs
        self.assertEqual(request["modelId"], "eu.amazon.nova-micro-v1:0")
        self.assertIn("Eugeny", request["system"][0]["text"])
        self.assertIn("server in AWS Stockholm or Yandex Cloud", request["system"][0]["text"])
        self.assertEqual(request["messages"][0]["content"][0]["text"], "hey there")

    def test_reply_tags_and_markdown_are_removed(self):
        self.converse.return_value = converse_response(
            text_block("<response>Привет, **Eugeny**! Я в порядке.</response>")
        )
        self.message("как дела?")

        self.assertEqual(self.http.texts(), ["Привет, Eugeny! Я в порядке."])

    def test_free_text_deploy_is_labelled(self):
        self.converse.return_value = converse_response(tool_block("deploy"))
        self.message("I need a VPN")

        self.assertEqual(self.button_labels(), ["✅ Yes, deploy", "❌ Cancel"])


class DirectRoutingTest(HandlerTestCase):
    def test_deploy_buttons_and_commands_dispatch_without_llm_or_confirmation(self):
        cases = {
            "deploy": ("aws", "wireguard"),
            "deploy openvpn": ("aws", "openvpn"),
            "Deploy Yandex": ("yandex", "wireguard"),
            "deploy yandex openvpn": ("yandex", "openvpn"),
            "/deploy": ("aws", "wireguard"),
            "/deploy_openvpn": ("aws", "openvpn"),
            "/deploy_yandex@SomeBot": ("yandex", "wireguard"),
            "/deploy_yandex_openvpn@SomeBot": ("yandex", "openvpn"),
        }
        for text, (cloud, protocol) in cases.items():
            with self.subTest(text=text):
                self.http.calls.clear()
                self.message(text)
                self.assertEqual(self.http.dispatches(), [{"action": "apply", "cloud": cloud, "protocol": protocol}])
                self.assertFalse([m for m in self.markups() if "inline_keyboard" in m])
        self.converse.assert_not_called()

    def test_destroy_buttons_and_commands_still_ask_for_confirmation(self):
        cases = {
            "destroy": "run:destroy:aws:wireguard",
            "destroy yandex": "run:destroy:yandex:wireguard",
            "/destroy": "run:destroy:aws:wireguard",
            "/destroy_yandex@SomeBot": "run:destroy:yandex:wireguard",
        }
        for text, data in cases.items():
            with self.subTest(text=text):
                self.http.calls.clear()
                self.message(text)
                self.assertEqual(self.keyboard(), [data, "cancel"])
                self.assertEqual(self.button_labels(), ["✅ Yes, destroy", "❌ Cancel"])
                self.assertEqual(self.http.dispatches(), [])
        self.converse.assert_not_called()

    def test_status_and_greetings_skip_the_llm(self):
        for text in ["status", "/status@SomeBot", "/why", "start", "/start", "hi", "Hi"]:
            with self.subTest(text=text):
                self.message(text)
        self.converse.assert_not_called()
        self.assertEqual(self.send_profiles.call_count, 2)
        self.assertEqual(self.http.texts().count("Long time no see, Eugeny!"), 4)

    def test_replies_carry_the_reply_keyboard(self):
        self.message("hi")

        markup = self.markups()[0]
        self.assertEqual(markup, handler.KEYBOARD)
        self.assertTrue(markup["is_persistent"])
        self.assertTrue(markup["resize_keyboard"])
        texts = [[button["text"] for button in row] for row in markup["keyboard"]]
        self.assertEqual(texts, [
            ["🚀 Deploy", "🔐 Deploy OpenVPN"],
            ["🚀 Deploy Yandex", "🔐 Deploy Yandex OpenVPN"],
            ["📊 Status", "💥 Destroy", "💥 Destroy Yandex"],
        ])

    def test_keyboard_buttons_run_their_commands_without_llm(self):
        deploys = {
            "🚀 Deploy": ("aws", "wireguard"),
            "🔐 Deploy OpenVPN": ("aws", "openvpn"),
            "🚀 Deploy Yandex": ("yandex", "wireguard"),
            "🔐 Deploy Yandex OpenVPN": ("yandex", "openvpn"),
        }
        for label, (cloud, protocol) in deploys.items():
            with self.subTest(label=label):
                self.http.calls.clear()
                self.message(label)
                self.assertEqual(self.http.dispatches(), [{"action": "apply", "cloud": cloud, "protocol": protocol}])

        for label, data in {"💥 Destroy": "run:destroy:aws:wireguard", "💥 Destroy Yandex": "run:destroy:yandex:wireguard"}.items():
            with self.subTest(label=label):
                self.http.calls.clear()
                self.message(label)
                self.assertEqual(self.keyboard(), [data, "cancel"])
                self.assertEqual(self.http.dispatches(), [])

        self.message("📊 Status")
        self.send_profiles.assert_called_once_with(CHAT_ID)
        self.converse.assert_not_called()


class FallbackTest(HandlerTestCase):
    def test_bedrock_error_falls_back_to_keywords(self):
        self.converse.side_effect = RuntimeError("ThrottlingException")
        self.message("please deploy openvpn")

        self.assertEqual(self.keyboard(), ["run:apply:aws:openvpn", "cancel"])
        self.assertEqual(self.http.dispatches(), [])

    def test_timeout_falls_back_to_keywords(self):
        self.converse.side_effect = TimeoutError("Read timeout on endpoint URL")
        self.message("hey you")

        self.assertEqual(self.http.texts(), ["Long time no see, Eugeny!"])

    def test_empty_response_falls_back_to_keywords(self):
        self.converse.return_value = converse_response(text_block("<thinking>Hmm"))
        self.message("please destroy yandex")

        self.assertEqual(self.keyboard(), ["run:destroy:yandex:wireguard", "cancel"])

    def test_content_filter_is_retried_with_a_forced_tool_call(self):
        self.converse.side_effect = [filtered_response(), converse_response(tool_block("destroy", cloud="yandex"))]
        self.message("убери сервер в яндексе")

        self.assertEqual(self.keyboard(), ["run:destroy:yandex:wireguard", "cancel"])
        self.assertNotIn("blocked", " ".join(self.http.texts()))
        first, retry = [c.kwargs for c in self.converse.call_args_list]
        self.assertEqual(first["toolConfig"]["toolChoice"], {"auto": {}})
        self.assertEqual(retry["toolConfig"]["toolChoice"], {"any": {}})

    def test_filtered_retry_falls_back_to_keywords(self):
        self.converse.side_effect = [converse_response(text_block("<thinking>Which server?</thinking>")), filtered_response()]
        self.message("please deploy openvpn")

        self.assertEqual(self.converse.call_count, 2)
        self.assertEqual(self.keyboard(), ["run:apply:aws:openvpn", "cancel"])
        self.assertNotIn("blocked", " ".join(self.http.texts()))

    def test_unknown_tool_falls_back_to_keywords(self):
        self.converse.return_value = converse_response(tool_block("rm_rf"))
        self.message("what is the status")

        self.send_profiles.assert_called_once_with(CHAT_ID)

    def test_without_model_only_keywords_work(self):
        with mock.patch.object(handler, "BEDROCK_MODEL_ID", ""):
            self.message("thanks!")

        self.converse.assert_not_called()
        self.assertEqual(self.http.texts(), ["I know you’d do the same for me."])


class ConfirmationTest(HandlerTestCase):
    def test_confirm_dispatches_the_workflow(self):
        self.callback("run:apply:yandex:openvpn")

        self.assertEqual(self.http.dispatches(), [{"action": "apply", "cloud": "yandex", "protocol": "openvpn"}])
        self.assertEqual(self.http.telegram("answerCallbackQuery"), [{"callback_query_id": "cb1"}])
        edits = self.http.telegram("editMessageText")
        self.assertEqual(edits[0]["text"], "Deploy OpenVPN in Yandex Cloud: confirmed.")
        self.assertNotIn("reply_markup", edits[0])
        self.assertEqual(self.http.texts(), ["The job is launched (apply openvpn in Yandex Cloud), please track the status."])

    def test_cancel_does_not_dispatch(self):
        self.callback("cancel")

        self.assertEqual(self.http.dispatches(), [])
        self.assertEqual(self.http.telegram("editMessageText")[0]["text"], "Cancelled.")

    def test_expired_confirmation_does_not_dispatch(self):
        self.callback("run:destroy:aws:wireguard", age=handler.CONFIRM_TTL + 1)

        self.assertEqual(self.http.dispatches(), [])
        self.assertEqual(self.http.telegram("editMessageText")[0]["text"], "Expired, please ask again.")

    def test_tampered_data_does_not_dispatch(self):
        for data in ["run:apply:gcp:wireguard", "run:reboot:aws:wireguard", "run:apply:aws", "run:apply:aws:x:y"]:
            with self.subTest(data=data):
                self.callback(data)
        self.assertEqual(self.http.dispatches(), [])

    def test_foreign_chat_is_ignored(self):
        self.callback("run:apply:aws:wireguard", chat_id=666)

        self.assertEqual(self.http.calls, [])

    def test_foreign_chat_message_is_ignored(self):
        self.message("deploy", chat_id=666)

        self.converse.assert_not_called()
        self.assertEqual(self.http.calls, [])

    def test_wrong_webhook_secret_is_rejected(self):
        response = self.call({"callback_query": {}}, secret="wrong")

        self.assertEqual(response, {"statusCode": 401})
        self.assertEqual(self.http.calls, [])


class ExplainFailureTest(HandlerTestCase):
    RUN = {
        "display_title": "apply wireguard in aws",
        "status": "completed",
        "conclusion": "failure",
        "html_url": "https://github.com/owner/repo/actions/runs/1",
        "jobs_url": "https://api.github.com/repos/owner/repo/actions/runs/1/jobs",
    }
    JOB = {
        "id": 99,
        "name": "deploy",
        "conclusion": "failure",
        "html_url": "https://github.com/owner/repo/actions/runs/1/job/99",
        "steps": [{"name": "Terraform init", "conclusion": "success"}, {"name": "Terraform apply", "conclusion": "failure"}],
    }
    LOG = (
        "2026-10-15T12:00:00.0000000Z Terraform apply\n"
        "2026-10-15T12:00:01.0000000Z \x1b[31mError: creating EC2 Instance: VcpuLimitExceeded\x1b[0m\n"
    )

    def setUp(self):
        super().setUp()
        self.http.responses[handler.GH_WORKFLOW_URL + "/runs"] = (200, {"workflow_runs": [self.RUN]})
        self.http.responses[self.RUN["jobs_url"]] = (200, {"jobs": [self.JOB]})
        patcher = mock.patch.object(handler, "gh_job_log", return_value=self.LOG)
        self.gh_job_log = patcher.start()
        self.addCleanup(patcher.stop)

    def test_failure_is_summarized_without_tools(self):
        summary = "The AWS vCPU quota is exhausted. Request a quota increase."
        self.converse.side_effect = [
            converse_response(tool_block("explain_failure")),
            converse_response(text_block(summary)),
        ]
        self.message("why did it fail?")

        self.gh_job_log.assert_called_once_with(99)
        explain_request = self.converse.call_args_list[1].kwargs
        self.assertNotIn("toolConfig", explain_request)
        log = explain_request["messages"][0]["content"][0]["text"]
        self.assertIn("Error: creating EC2 Instance: VcpuLimitExceeded", log)
        self.assertNotIn("2026-10-15T", log)
        self.assertNotIn("\x1b", log)
        self.assertEqual(self.http.texts(), [
            f'apply wireguard in aws: deploy failed at "Terraform apply".\n\n{summary}\n\n{self.JOB["html_url"]}'
        ])

    def test_without_llm_the_log_tail_is_sent(self):
        self.converse.side_effect = RuntimeError("AccessDeniedException")
        self.message("/why")

        sent = self.http.telegram("sendMessage")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        self.assertIn("<pre>Terraform apply\nError: creating EC2 Instance: VcpuLimitExceeded</pre>", sent[0]["text"])

    def test_successful_run_is_not_explained(self):
        self.http.responses[handler.GH_WORKFLOW_URL + "/runs"] = (200, {"workflow_runs": [dict(self.RUN, conclusion="success")]})
        self.message("/why")

        self.gh_job_log.assert_not_called()
        self.assertEqual(self.http.texts(), ["The last job did not fail.\nLast job: apply wireguard in aws\nState: success"])


class ReadyNotificationTest(HandlerTestCase):
    def s3_event(self, *keys):
        records = [{"eventSource": "aws:s3", "s3": {"object": {"key": key}}} for key in keys]
        return handler.handler({"Records": records}, None)

    def test_ready_marker_sends_the_profiles_of_that_cloud(self):
        self.assertEqual(self.s3_event("profiles/aws/ready"), {"statusCode": 200})
        self.assertEqual(self.http.texts(), ["The VPN server in AWS Stockholm is ready."])
        self.assertEqual(self.send_profiles.call_args_list, [mock.call(CHAT_ID, clouds={"aws"})])

    def test_other_objects_do_not_notify(self):
        self.s3_event("profiles/aws/phone.conf", "profiles/aws/phone.png", "profiles/gcp/ready", "terraform/ready")
        self.assertEqual(self.http.texts(), [])
        self.send_profiles.assert_not_called()

    def test_every_allowed_chat_is_notified(self):
        with mock.patch.object(handler, "ALLOWED_CHAT_IDS", {42, 7}):
            self.s3_event("profiles/yandex/ready")
        self.assertEqual(
            self.send_profiles.call_args_list,
            [mock.call(7, clouds={"yandex"}), mock.call(42, clouds={"yandex"})],
        )


class SendProfilesTest(unittest.TestCase):
    def test_only_the_requested_cloud_is_sent(self):
        keys = ["profiles/aws/phone.conf", "profiles/aws/ready", "profiles/yandex/client.ovpn", "profiles/yandex/ready"]
        http = FakeHttp()
        listing = {"Contents": [{"Key": k} for k in keys]}
        with mock.patch.object(handler, "http_request", http), \
                mock.patch.object(handler.S3_CLIENT, "list_objects_v2", return_value=listing), \
                mock.patch.object(handler.S3_CLIENT, "generate_presigned_url", return_value="https://s3/link"):
            handler.send_profiles(CHAT_ID, clouds={"yandex"})
        texts = http.texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("Yandex Cloud", texts[0])
        self.assertIn("client.ovpn", texts[0])
        self.assertNotIn("ready", texts[0])


class AwsLabelTest(unittest.TestCase):
    def test_label_follows_the_region(self):
        self.assertEqual(handler.CLOUDS["aws"], "AWS Stockholm")
        self.assertEqual(handler.aws_label("us-east-1"), "AWS N. Virginia")

    def test_unknown_region_is_shown_as_is(self):
        self.assertEqual(handler.aws_label("mx-central-1"), "AWS mx-central-1")

    def test_location_overrides_the_region(self):
        self.assertEqual(handler.aws_label("eu-central-1", "Frankfurt am Main"), "AWS Frankfurt am Main")


class JobLogTest(unittest.TestCase):
    def test_redirect_is_followed_without_the_github_token(self):
        blob_url = "https://blob.example/logs/99?sig=abc"
        redirect = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/actions/jobs/99/logs", 302, "Found", {"Location": blob_url}, None
        )
        with mock.patch.object(handler.NO_REDIRECT_OPENER, "open", side_effect=redirect) as gh_open, \
                mock.patch.object(handler.urllib.request, "urlopen", return_value=io.BytesIO(b"log line")) as blob_open:
            self.assertEqual(handler.gh_job_log(99), "log line")

        self.assertEqual(gh_open.call_args.args[0].get_header("Authorization"), "Bearer gh-token")
        self.assertEqual(blob_open.call_args.args[0], blob_url)

    def test_long_log_is_cut_at_a_line_boundary(self):
        log = "\n".join(f"line {i}" for i in range(5000))
        tail = handler.log_tail(log)

        self.assertLessEqual(len(tail), handler.LOG_TAIL_CHARS)
        self.assertTrue(tail.startswith("line "))
        self.assertTrue(tail.endswith("line 4999"))


if __name__ == "__main__":
    unittest.main()

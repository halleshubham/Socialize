"""process_pending_emails must dispatch each email as its own background
task rather than looping through them on one thread - found live: one
email whose auto-advance reached reel generation blocked every other email
in the same fetch behind it on that one thread, showing as the whole batch
stuck "processing" on the board."""

from unittest.mock import patch

from backend.app.worker import pipeline


def test_dispatches_one_background_task_per_email():
    email_ids = ["a", "b", "c"]
    with patch.object(pipeline, "run_in_background") as mock_run:
        pipeline.process_pending_emails(email_ids)

    assert mock_run.call_count == len(email_ids)
    dispatched_ids = [call.args[1] for call in mock_run.call_args_list]
    assert dispatched_ids == email_ids
    for call in mock_run.call_args_list:
        assert call.args[0] is pipeline._process_one_email


def test_empty_batch_dispatches_nothing():
    with patch.object(pipeline, "run_in_background") as mock_run:
        pipeline.process_pending_emails([])
    mock_run.assert_not_called()


def test_one_email_failing_does_not_raise_or_block_others():
    with patch.object(pipeline, "process_email", side_effect=ValueError("boom")):
        # Must not raise - a failed email is logged, not propagated, so it
        # can never take the rest of the batch (or the caller) down with it.
        pipeline._process_one_email("whatever")

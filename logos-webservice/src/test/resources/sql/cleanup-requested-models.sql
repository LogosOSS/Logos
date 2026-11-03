DELETE FROM requested_model_votes;
DELETE FROM requested_models;
-- The first vote for a gated model also publishes a notification; this class
-- seeds none, so a full wipe of the rows the tests created is complete.
DELETE FROM notification_recipients;
DELETE FROM notifications;

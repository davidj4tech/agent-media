;;; am-control-test.el --- ERT tests for the control contract -*- lexical-binding: t; -*-

;;; Commentary:

;; The contract is tested with `media' stubbed out: what matters is that the
;; right argv reaches the right host, not that agent-media works (it has its
;; own suite).  Nothing here touches a real player, so the tests are safe to
;; run on a host where music is actually playing — which the Python suite
;; learned the hard way (packages/core/tests/conftest.py scrubs the phone
;; endpoint for the same reason).
;;
;; Run:  emacs -Q --batch -L lisp -l tests/am-control-test.el \
;;             -f ert-run-tests-batch-and-exit

;;; Code:

(require 'ert)
(require 'cl-lib)
(require 'am-control)
(require 'am-control-hold)

(defvar am-test--runs nil
  "Commands captured instead of executed.")

(defun am-test--capture (_action argv) (push argv am-test--runs) nil)

(defmacro am-test-with-capture (&rest body)
  "Run BODY with dispatch captured into `am-test--runs' (newest first)."
  `(let ((am-test--runs nil)
         (am-control-local-command '("media"))
         (am-control-remote-command '("ssh" "red5" "media"))
         (am-control-local-hold-command '("media-call-guard"))
         (am-control-remote-hold-command '("ssh" "red5" "media-call-guard"))
         (am-control-local-actions '(toggle next prev seek hold release status))
         (am-control-hold--depth 0))
     (cl-letf (((symbol-function 'am-control--run) #'am-test--capture))
       ,@body
       (nreverse am-test--runs))))


;;; Dispatch routing — the heart of the design

(ert-deftest am-control-test-transport-is-local ()
  "Transport actions never leave the host: that is the whole latency win."
  (let ((runs (am-test-with-capture
               (am-control-toggle) (am-control-next) (am-control-prev))))
    (should (equal (nth 0 runs) '("media" "music" "toggle")))
    (should (equal (nth 1 runs) '("media" "music" "next")))
    (should (equal (nth 2 runs) '("media" "music" "prev")))))

(ert-deftest am-control-test-play-is-remote ()
  "play/queue-add need the library and history, so they go to red5."
  (let ((runs (am-test-with-capture
               (am-control-play "yt:https://x")
               (am-control-queue-add "yt:https://y"))))
    (should (equal (nth 0 runs)
                   '("ssh" "red5" "media" "music" "play" "yt:https://x")))
    (should (equal (nth 1 runs)
                   '("ssh" "red5" "media" "music" "play" "--add" "yt:https://y")))))

(ert-deftest am-control-test-play-options ()
  (let ((runs (am-test-with-capture
               (am-control-play "u" "phone" "audiobook"))))
    (should (equal (car runs)
                   '("ssh" "red5" "media" "music" "play"
                     "--where" "phone" "--as" "audiobook" "u")))))

(ert-deftest am-control-test-local-actions-nil-forces-remote ()
  "The escape hatch: nil local-actions reproduces a pure red5 surface."
  (let* ((am-control-local-actions nil)
         (runs (am-test-with-capture
                (let ((am-control-local-actions nil))
                  (am-control-toggle)))))
    (should (equal (car runs) '("ssh" "red5" "media" "music" "toggle")))))

(ert-deftest am-control-test-seek-forms ()
  (let ((runs (am-test-with-capture
               (am-control-seek "+90")
               (am-control-seek-forward)
               (am-control-seek-backward 15))))
    (should (equal (nth 0 runs) '("media" "music" "seek" "+90")))
    (should (equal (nth 1 runs) '("media" "music" "seek" "+30")))
    (should (equal (nth 2 runs) '("media" "music" "seek" "-15")))))

(ert-deftest am-control-test-prev-restart ()
  (let ((runs (am-test-with-capture (am-control-prev t))))
    (should (equal (car runs) '("media" "music" "prev" "--restart-first")))))


;;; Hold — the call-guard action

(ert-deftest am-control-test-hold-uses-call-guard ()
  "hold/release map onto call-guard, not onto a pause of the music sink.
Music ducks and speech pauses; that split is the pipeline's policy."
  (let ((runs (am-test-with-capture (am-control-hold) (am-control-release))))
    (should (equal (nth 0 runs) '("media-call-guard" "--hold")))
    (should (equal (nth 1 runs) '("media-call-guard" "--release")))))

(ert-deftest am-control-test-hold-is-idempotent ()
  "Holding twice is one hold; the inner release must not un-duck early."
  (let ((runs (am-test-with-capture
               (am-control-hold) (am-control-hold)
               (am-control-release))))
    (should (equal runs '(("media-call-guard" "--hold"))))))

(ert-deftest am-control-test-with-hold-releases-on-error ()
  "A C-g or error during a voice chat must not strand the hold."
  (let ((runs (am-test-with-capture
               (ignore-errors
                 (am-control-with-hold (error "boom"))))))
    (should (equal runs '(("media-call-guard" "--hold")
                          ("media-call-guard" "--release"))))))

(ert-deftest am-control-test-with-hold-nests ()
  (let ((runs (am-test-with-capture
               (am-control-with-hold
                 (am-control-with-hold (ignore))))))
    (should (equal runs '(("media-call-guard" "--hold")
                          ("media-call-guard" "--release"))))))


;;; Status parsing

(ert-deftest am-control-test-status-parses-json ()
  (cl-letf (((symbol-function 'am-control--run-sync)
             (lambda (_a _argv)
               (concat "{\"backend\":\"phone\",\"uri\":\"/x.mka\","
                       "\"title\":\"T\",\"chapter\":null,\"pos_ms\":1000,"
                       "\"dur_ms\":2000,\"paused\":false,\"speed\":1.0,"
                       "\"volume\":130,\"held\":true}"))))
    (let ((s (am-control-status)))
      (should (equal (plist-get s :backend) "phone"))
      (should (equal (plist-get s :title) "T"))
      (should (equal (plist-get s :pos-ms) 1000))
      (should (eq (plist-get s :paused) nil))
      (should (eq (plist-get s :held) t))
      (should (equal (plist-get s :volume) 130)))))

(ert-deftest am-control-test-status-survives-garbage ()
  "A poller must never see a traceback, and never wedge on bad input."
  (cl-letf (((symbol-function 'am-control--run-sync) (lambda (_a _v) "not json")))
    (should (null (am-control-status))))
  (cl-letf (((symbol-function 'am-control--run-sync) (lambda (_a _v) nil)))
    (should (null (am-control-status)))
    (should (equal (am-control-now-string) "music: unreachable"))))

(ert-deftest am-control-test-now-string-forms ()
  (cl-letf (((symbol-function 'am-control-status)
             (lambda () '(:title nil :paused nil :held nil))))
    (should (equal (am-control-now-string) "music: idle")))
  (cl-letf (((symbol-function 'am-control-status)
             (lambda () '(:title "T" :paused t :held t))))
    (should (equal (am-control-now-string) "⏸ T [held]"))))

(provide 'am-control-test)
;;; am-control-test.el ends here

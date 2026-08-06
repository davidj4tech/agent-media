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
;; Needed for its defcustoms to be known special vars before we let-bind them.
(require 'am-control-mpv)

(defvar am-test--runs nil
  "Commands captured instead of executed.")

(defun am-test--capture (_action argv) (push argv am-test--runs) nil)

(defvar am-test--prefer-direct nil
  "Value `am-control-prefer-direct' takes inside `am-test-with-capture'.
Default nil so CLI-shape tests assert the CLI; the fast-path tests bind it.")

(defmacro am-test-with-capture (&rest body)
  "Run BODY with dispatch captured into `am-test--runs' (newest first).

`am-control-prefer-direct' is nil here so these assert the CLI argv, which
is what they are about; the fast path has its own tests that opt in.

`am-control-hold-flag' is pointed at a temp path unconditionally. Without
that, a hold test would touch the REAL call-guard flag and duck the machine
running the suite — the elisp version of the mistake conftest.py guards
against on the Python side."
  `(let* ((am-test--runs nil)
          (am-test--flagdir (make-temp-file "am-test-flag" t))
          (am-control-hold-flag (expand-file-name "call-guard.hold"
                                                  am-test--flagdir))
          (am-control-prefer-direct am-test--prefer-direct)
          (am-control-local-command '("media"))
          (am-control-remote-command '("ssh" "red5" "media"))
          (am-control-local-hold-command '("media-call-guard"))
          (am-control-remote-hold-command '("ssh" "red5" "media-call-guard"))
          (am-control-local-actions '(toggle next prev seek hold release status))
          (am-control-hold--depth 0))
     (unwind-protect
         (cl-letf (((symbol-function 'am-control--run) #'am-test--capture))
           ,@body
           (nreverse am-test--runs))
       (delete-directory am-test--flagdir t))))


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

;;; Direct mpv fast path
;;
;; The fast path must be a pure optimisation: same effect, or it declines and
;; the CLI runs. These pin the decision, not the IPC (that is exercised against
;; a real mpv in tests/run-mpv-integration.sh).

(defmacro am-test-with-direct (usable &rest body)
  "Run BODY with the direct path reporting USABLE and mpv commands captured."
  `(let ((am-mpv-calls nil))
     (cl-letf (((symbol-function 'am-control-mpv-usable-p) (lambda () ,usable))
               ((symbol-function 'am-control-mpv-command)
                (lambda (&rest a) (push a am-mpv-calls) '(t)))
               ((symbol-function 'am-control-mpv-invalidate) #'ignore))
       (let ((cli (let ((am-test--prefer-direct t)) (am-test-with-capture ,@body))))
         (list :mpv (nreverse am-mpv-calls) :cli cli)))))

(ert-deftest am-control-test-direct-transport-skips-cli ()
  "When mpv is reachable, transport goes direct and spawns nothing."
  (let ((r (am-test-with-direct t (am-control-toggle) (am-control-next))))
    (should (equal (plist-get r :mpv)
                   '(("cycle" "pause") ("playlist-next" "weak"))))
    (should (null (plist-get r :cli)))))

(ert-deftest am-control-test-direct-declines-to-cli ()
  "Unreachable mpv must fall back, not fail."
  (let ((r (am-test-with-direct nil (am-control-toggle))))
    (should (null (plist-get r :mpv)))
    (should (equal (plist-get r :cli) '(("media" "music" "toggle"))))))

(ert-deftest am-control-test-remote-action-never-goes-direct ()
  "An action routed remote must honour that, not short-circuit locally."
  (let* ((am-control-local-actions nil)
         (r (am-test-with-direct t
              (let ((am-control-local-actions nil)) (am-control-toggle)))))
    (should (null (plist-get r :mpv)))
    (should (equal (plist-get r :cli) '(("ssh" "red5" "media" "music" "toggle"))))))

(ert-deftest am-control-test-seek-fast-path-only-simple-forms ()
  "Timecodes keep the CLI's parsing rather than a reimplementation."
  (let ((r (am-test-with-direct t (am-control-seek "+30") (am-control-seek "1:23:45"))))
    (should (equal (plist-get r :mpv) '(("seek" 30 "relative"))))
    (should (equal (plist-get r :cli) '(("media" "music" "seek" "1:23:45"))))))

(ert-deftest am-control-test-prev-restart-never-fast-pathed ()
  "--restart-first carries real logic; it must reach the CLI."
  (let ((r (am-test-with-direct t (am-control-prev t))))
    (should (null (plist-get r :mpv)))
    (should (equal (plist-get r :cli)
                   '(("media" "music" "prev" "--restart-first"))))))

(ert-deftest am-control-test-relative-seconds-parsing ()
  (should (equal (am-control--relative-seconds "+90") 90))
  (should (equal (am-control--relative-seconds "-15") -15))
  (should (null (am-control--relative-seconds "1:23")))
  (should (null (am-control--relative-seconds "90")))     ; absolute
  (should (null (am-control--relative-seconds "-5:00"))))


;;; Hold via the flag file

(ert-deftest am-control-test-hold-touches-flag-directly ()
  "Ducking is the barge-in critical path: it must not spawn Python.
Uses the harness's temp flag (it rebinds `am-control-hold-flag' itself, so
the assertions read that binding rather than one of their own)."
  (let* ((am-test--prefer-direct t)
         (spawned (am-test-with-capture
                   (am-control-hold)
                   (should (am-control-hold-flag-present-p))
                   (am-control-release)
                   (should-not (am-control-hold-flag-present-p)))))
    (should (null spawned))))

(ert-deftest am-control-test-hold-falls-back-when-remote ()
  "A hold routed to the remote host must run there, not touch a local file."
  (let* ((am-control-local-actions nil)
         (am-control-hold--depth 0)
         (runs (am-test-with-capture
                (let ((am-control-local-actions nil)) (am-control-hold)))))
    (should (equal runs '(("ssh" "red5" "media-call-guard" "--hold"))))))

(ert-deftest am-control-test-hold-release-is-idempotent-on-disk ()
  "Releasing an already-released hold must not error."
  (let* ((tmp (make-temp-file "am-hold" t))
         (am-control-hold-flag (expand-file-name "call-guard.hold" tmp))
         (am-control-hold--depth 1))
    (unwind-protect
        (progn (am-control-release)
               (setq am-control-hold--depth 1)
               (should (progn (am-control-release) t)))
      (delete-directory tmp t))))

(ert-deftest am-control-test-hold-survives-a-cold-mpv-module ()
  "A fresh daemon whose FIRST action is a hold must not hit a void variable.
`am-control-prefer-direct' is defined in am-control-mpv, which loads on
demand.  Every other test in this file runs under `am-test-with-capture',
which binds that variable — so only an explicitly cold call reaches the path
a just-started daemon takes, which is how this shipped broken."
  (when (featurep 'am-control-mpv)
    (unload-feature 'am-control-mpv t))
  (makunbound 'am-control-prefer-direct)
  (should (progn (am-control-hold--direct-p) t)))

(provide 'am-control-test)
;;; am-control-test.el ends here

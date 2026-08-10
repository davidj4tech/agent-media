;;; am-read.el --- Read a document, a buffer or a region aloud -*- lexical-binding: t; -*-

;; Author: David
;; Package-Requires: ((emacs "27.1"))

;;; Commentary:

;; Selection belongs to Emacs; speaking belongs to agent-media.
;;
;; agent-media grew its own document picker — roots, filters, fuzzy matching,
;; project scoping, recency ranking — and every one of those is a worse copy of
;; something already here.  `consult' and `telescope' match better than an fzf
;; wrapper; `project.el' already knows the project root; org-roam already has
;; the notes indexed with tags and backlinks.  What agent-media has that no
;; editor does is the rest of the pipeline: a projection that announces code
;; blocks and tables instead of reading them aloud, headings rendered as
;; chapter marks, a resume position, and playback that lands on a phone in
;; another hemisphere.
;;
;; So the contract is a *path or a piece of text*, never a picker:
;;
;;     media doc play <path>
;;     media doc play --stdin --fmt org --title "..."   < the region
;;
;; Bind these wherever selection already happens — dired, `org-roam-node-find',
;; the current buffer — and the picker stops being a thing to maintain.
;;
;; Shelling out goes through `am-control--media', per am-control.el's rule that
;; it is the only file permitted to run the CLI.  This file adds verbs; it does
;; not add a second way to talk to agent-media.

;;; Code:

(require 'am-control)

;; Optional at compile time: org-roam supplies the node picker when it is
;; installed, and dired's map only exists once dired has loaded.
(defvar dired-mode-map)
(declare-function dired-get-filename "dired" (&optional localp no-error-if-not-filep))
(declare-function org-roam-node-read "org-roam-node" (&optional initial-input filter-fn sort-fn require-match prompt))
(declare-function org-roam-node-file "org-roam-node" (node))

(defgroup am-read nil
  "Read documents aloud through agent-media."
  :group 'am-control)

(defcustom am-read-target ""
  "Playback target: empty follows the speech lane, else rooms/local/phone."
  :type 'string
  :group 'am-read)

(defun am-read--fmt ()
  "Markup of the current buffer, as the projection names it."
  (if (derived-mode-p 'org-mode) "org" "md"))

(defun am-read--args (&rest args)
  (append (list "doc" "play")
          (when (and am-read-target (not (string-empty-p am-read-target)))
            (list "--target" am-read-target))
          args))

;;;###autoload
(defun am-read-file (path)
  "Read the document at PATH aloud, with its headings as chapters.

Interactively this defaults to whatever the context already points at — the
file under point in dired, else the file being visited — because that is the
selection Emacs has already made and asking again would be the whole mistake
this file exists to undo."
  (interactive
   (list (read-file-name
          "Read aloud: " nil
          (or (and (derived-mode-p 'dired-mode)
                   (fboundp 'dired-get-filename)
                   (ignore-errors (dired-get-filename nil t)))
              (buffer-file-name))
          t
          (when (and (derived-mode-p 'dired-mode)
                     (fboundp 'dired-get-filename))
            (ignore-errors (file-name-nondirectory
                            (dired-get-filename nil t)))))))
  (let ((full (expand-file-name path)))
    (unless (file-readable-p full)
      (user-error "am-read: cannot read %s" full))
    (message "am-read: rendering %s…" (file-name-nondirectory full))
    (apply #'am-control--media 'doc-read (am-read--args full))))

(defun am-read--send-text (text title fmt)
  "Pipe TEXT to the reader as TITLE, treating it as FMT markup.

Sent on stdin rather than written to a temporary file: the text is a region or
a buffer, which is not a file and should not have to become one.  The process
is started here rather than through `am-control--media' only because it needs
a stdin — the command line it runs is the same one."
  (when (string-empty-p (string-trim text))
    (user-error "am-read: nothing to read"))
  (let* ((argv (append (am-control--prefix 'doc-read 'media)
                       (am-read--args "--stdin" "--fmt" fmt
                                      "--title" (or title "Selection"))))
         (proc (make-process
                :name "am-read-text"
                :command argv
                :noquery t
                :connection-type 'pipe
                :sentinel
                (lambda (p event)
                  (let ((status (process-exit-status p)))
                    (when (and (memq (process-status p) '(exit signal))
                               (not (zerop status)))
                      (message "am-read: failed (%s) %s"
                               status (string-trim (or event "")))))))))
    (process-send-string proc text)
    (process-send-eof proc)
    (message "am-read: rendering %s…" (or title "selection"))
    proc))

;;;###autoload
(defun am-read-region (beg end)
  "Read the region aloud."
  (interactive "r")
  (am-read--send-text (buffer-substring-no-properties beg end)
                      (format "%s (region)" (buffer-name))
                      (am-read--fmt)))

;;;###autoload
(defun am-read-buffer ()
  "Read the current buffer aloud.

Uses the buffer text, not the file on disk, so unsaved edits are what you
hear — otherwise the obvious use (write a paragraph, listen to it) reads the
previous version and sounds like a caching bug."
  (interactive)
  (am-read--send-text (buffer-substring-no-properties (point-min) (point-max))
                      (buffer-name)
                      (am-read--fmt)))

;;;###autoload
(defun am-read-dwim ()
  "Read the region if there is one, else the buffer's file, else the buffer."
  (interactive)
  (cond ((use-region-p) (am-read-region (region-beginning) (region-end)))
        ((and (buffer-file-name) (not (buffer-modified-p)))
         (am-read-file (buffer-file-name)))
        (t (am-read-buffer))))

;;;###autoload
(defun am-read-org-node ()
  "Pick an org-roam node and read it aloud.

The point of the whole rearrangement: org-roam already has 800-odd notes
indexed with their tags and backlinks, and its own completion over them.  This
adds a verb to that, rather than a second index."
  (interactive)
  (unless (fboundp 'org-roam-node-read)
    (user-error "am-read: org-roam is not available"))
  (let ((node (org-roam-node-read)))
    (am-read-file (org-roam-node-file node))))

;;;###autoload
(defun am-read-agenda ()
  "Speak today's agenda as a short briefing."
  (interactive)
  (message "am-read: building the agenda…")
  (apply #'am-control--media 'doc-read (list "doc" "agenda")))

;;;###autoload
(defun am-read-setup-bindings ()
  "Bind the reader where selection already happens.

Not done on load: a package that takes keys in another mode's map without
being asked is a package you end up fighting."
  (interactive)
  (with-eval-after-load 'dired
    (define-key dired-mode-map (kbd "C-c r") #'am-read-file))
  (global-set-key (kbd "C-c a r") #'am-read-dwim)
  (global-set-key (kbd "C-c a n") #'am-read-org-node)
  (global-set-key (kbd "C-c a g") #'am-read-agenda))

(provide 'am-read)
;;; am-read.el ends here

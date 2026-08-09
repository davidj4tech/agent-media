;;; agenda-export.el --- dump agenda entries as JSON for agent-media  -*- lexical-binding: t; -*-
;;
;; Asked over emacsclient by `media doc agenda`. Emacs is the source of truth
;; here on purpose: org-agenda-files, the TODO keyword set, and which states
;; count as done are all user configuration, and a parser on our side would be
;; a second, quietly diverging copy of those rules. This just reads what Emacs
;; already knows and writes it out as data.
;;
;; Deliberately writes to a file rather than returning a value: emacsclient
;; --eval round-trips through the shell's quoting twice, and a large sexp of
;; user text (apostrophes, quotes, brackets) does not survive that reliably.

(require 'org)
(require 'json)

(defun media-agenda--entry ()
  "One agenda entry as an alist, or nil when the heading has no TODO state."
  (let ((state (org-get-todo-state)))
    (when state
      (list (cons 'todo state)
            (cons 'done (and (member state org-done-keywords) t))
            (cons 'heading (or (nth 4 (org-heading-components)) ""))
            (cons 'priority (let ((p (nth 3 (org-heading-components))))
                              (if p (char-to-string p) "")))
            (cons 'tags (or (org-get-tags) []))
            (cons 'scheduled (org-entry-get nil "SCHEDULED"))
            (cons 'deadline (org-entry-get nil "DEADLINE"))
            (cons 'file (file-name-nondirectory
                         (or (buffer-file-name) "")))))))

(defun media-agenda-export (out)
  "Write every TODO-stateful entry across `org-agenda-files' to OUT as JSON."
  (let ((entries '()))
    (dolist (f (org-agenda-files))
      (when (file-readable-p f)
        (with-current-buffer (find-file-noselect f t)
          (org-with-wide-buffer
           (goto-char (point-min))
           (while (re-search-forward org-heading-regexp nil t)
             (let ((e (media-agenda--entry)))
               (when e (push e entries))))))))
    (with-temp-file out
      (insert (json-encode (nreverse entries))))
    (length entries)))

(provide 'agenda-export)
;;; agenda-export.el ends here

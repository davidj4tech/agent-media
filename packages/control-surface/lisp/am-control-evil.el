;;; am-control-evil.el --- Make the surface work in evil states  -*- lexical-binding: t; -*-

;;; Commentary:

;; Most of the surface already works under evil and needs nothing from this
;; file: `C-c m' passes through evil untouched, and a Spacemacs leader binding
;; (`SPC a m …') *is* a normal-state binding.
;;
;; The status buffer is the exception, and it is a genuine bug rather than a
;; missing nicety.  `*am-control: music*' is a `special-mode' buffer whose
;; footer advertises "g refresh  SPC toggle  n/p next/prev  h hold  q bury" —
;; and under evil every one of those is shadowed by normal state: `n' is
;; `evil-search-next', `p' is `evil-paste-after', `SPC' is `evil-forward-char',
;; `h' is `evil-backward-char', `q' records a macro, `g' is a prefix.  The
;; buffer documents keys that do not work.
;;
;; So the mode map is re-bound into evil's normal and motion states.  The
;; bindings are read back OUT of `am-adapter-empv-status-mode-map' rather than
;; retyped here, so a key added to the adapter is carried into evil for free
;; and the two lists cannot drift.
;;
;; A global evil prefix is offered but off by default — see
;; `am-control-evil-prefix'.  Which key is free is a property of your config,
;; and under Spacemacs the leader already covers this.

;;; Code:

(require 'am-control)

(defvar evil-normal-state-map)
(defvar evil-motion-state-map)
(declare-function evil-define-key* "evil-core")

(defcustom am-control-evil-prefix nil
  "Evil normal/motion-state prefix for the control surface, or nil.
A key description such as \"SPC m\" or \", m\", under which the adapter's
map is bound.  nil — the default — binds nothing globally: `C-c m' already
works in every evil state, and under Spacemacs `SPC' belongs to the leader,
where `am-control/bind-leader' puts these commands instead."
  :type '(choice (const :tag "None" nil) string)
  :group 'am-control)

(defun am-control-evil--rebind (map states)
  "Re-bind MAP's own keys into evil STATES so they survive normal state.
Bindings are collected before any are installed: `evil-define-key*' stores
its auxiliary keymap inside MAP, and mutating a keymap while walking it is
how you get a half-bound map."
  (let (pairs)
    (map-keymap
     (lambda (key def)
       ;; Evil stores its auxiliary keymaps INSIDE the map it augments, under
       ;; pseudo-keys named `normal state', `motion state' and so on.  Walking
       ;; them back in on a second call re-binds evil's own bookkeeping and
       ;; silently guts the map — `SPC' fell through to `scroll-up-command'.
       ;; Skipping them is what makes this function idempotent.
       (unless (or (keymapp def)
                   (and (symbolp key)
                        (string-match-p " state\\'" (symbol-name key))))
         (push (cons key def) pairs)))
     map)
    (dolist (pair (nreverse pairs))
      (evil-define-key* states map (vector (car pair)) (cdr pair)))))

(defun am-control-evil--bind-prefix (prefix map)
  "Bind MAP under PREFIX in evil's normal and motion state maps.
A prefix cannot be built on a key that already holds a command — `SPC' is
`evil-forward-char' in motion state — so the leading key is freed first, and
loudly: displacing a vim motion is exactly the sort of thing that should
appear in *Messages* rather than be discovered later."
  (let ((lead (vector (aref (kbd prefix) 0))))
    (dolist (state-map (list evil-normal-state-map evil-motion-state-map))
      (let ((existing (lookup-key state-map lead)))
        (when (and existing (not (keymapp existing)))
          (message "am-control: freeing %s in evil (was `%s')"
                   (key-description lead) existing)
          (define-key state-map lead nil)))
      (define-key state-map (kbd prefix) map))))

;;;###autoload
(defun am-control-evil-setup ()
  "Make the control surface usable from evil's normal and motion states.
Safe to call more than once, and safe to call before the adapter loads."
  (with-eval-after-load 'am-adapter-empv
    (am-control-evil--rebind (symbol-value 'am-adapter-empv-status-mode-map)
                             '(normal motion))
    (when am-control-evil-prefix
      (am-control-evil--bind-prefix am-control-evil-prefix
                                    (symbol-value 'am-adapter-empv-map)))))

(provide 'am-control-evil)
;;; am-control-evil.el ends here

// The zab console's sign-in (docs/ADMIN.md §4.4), its only script. The token is the link's
// fragment, which no server and no log sees: this copies it into the sign-in form, removes it
// from the address bar, and posts the form. It makes no other request.
(function () {
  "use strict";
  var token = window.location.hash.slice(1);
  if (!token) {
    return;
  }
  window.history.replaceState(null, "", window.location.pathname);
  document.getElementById("token").value = token;
  document.getElementById("login").submit();
})();

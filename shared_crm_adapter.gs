/**
 * Server-side adapter for Personalised message (library version 61).
 * Add this as a new .gs file in the OWNER'S Apps Script project.
 * No triggers are installed and no email is sent by this adapter.
 * Use with shared_outreach_hooks.gs and the reviewed sender patch.
 */
var SharedCRM = (function () {
  function call(role, method, path, payload) {
    var props = PropertiesService.getScriptProperties();
    var base = String(props.getProperty('SHARED_CRM_BASE_URL') || '').replace(/\/$/, '');
    var key = props.getProperty('SHARED_CRM_' + role.toUpperCase() + '_KEY');
    if (!/^https:\/\/[A-Za-z0-9.-]+(?::\d+)?(?:\/[A-Za-z0-9/_-]*)?$/.test(base) || !key || key.length < 32) {
      throw new Error('Configure the shared CRM HTTPS address and module service key in Script Properties.');
    }
    var options = {
      method: method,
      headers: { Authorization: 'Bearer ' + key },
      muteHttpExceptions: true,
      followRedirects: false
    };
    if (payload !== undefined) {
      options.contentType = 'application/json';
      options.payload = JSON.stringify(payload);
    }
    var response = UrlFetchApp.fetch(base + path, options);
    var code = response.getResponseCode();
    if (code < 200 || code >= 300) {
      // Do not log prospect data, referral tokens, keys, or response bodies.
      throw new Error('Shared CRM request failed (HTTP ' + code + ').');
    }
    return code === 204 ? null : JSON.parse(response.getContentText());
  }

  return {
    upsertProspect: function (externalId, contact, expectedVersion) {
      var payload = { external_id: externalId, contact: contact };
      if (expectedVersion !== undefined) payload.expected_version = expectedVersion;
      return call('discovery', 'post', '/v1/contacts', payload);
    },
    /** Map ONLY headings observed in the shared Prospects sheet; Sent is not qualification. */
    prospectFromSheetRow: function (headings, row, qualification) {
      var indexes = {};
      headings.forEach(function (heading, index) {
        var name = String(heading).trim().toLowerCase();
        if (Object.prototype.hasOwnProperty.call(indexes, name)) throw new Error('Duplicate sheet headings require review.');
        indexes[name] = index;
      });
      function cell(name) {
        return indexes[name] === undefined ? '' : String(row[indexes[name]] || '').trim();
      }
      if (!cell('email')) throw new Error('This prospect row needs an email address.');
      if (qualification !== 'pending' && qualification !== 'qualified' && qualification !== 'unqualified') {
        throw new Error('Pass the discovery module qualification explicitly; email Status does not prove qualification.');
      }
      return {name: cell('name'), company: cell('company'), job_title: cell('job title'),
              email: cell('email'), qualification: qualification};
    },
    createChatLink: function (contactId, campaignId) {
      return call('outreach', 'post', '/v1/chat-links', {contact_id: contactId, campaign_id: campaignId});
    },
    getContact: function (contactId) {
      return call('outreach', 'get', '/v1/contacts/' + encodeURIComponent(contactId));
    },
    reserveEmail: function (contactId, campaignId, eventId, version) {
      return call('outreach', 'post', '/v1/email-reservations', {
        contact_id: contactId, campaign_id: campaignId, event_id: eventId,
        snapshot_version: version, reviewed: true
      });
    },
    recordOutcome: function (uniqueEventId, outcome) {
      return call('outreach', 'post', '/v1/communications', {external_id: uniqueEventId, outcome: outcome});
    },
    currentSnapshot: function () {
      return call('outreach', 'get', '/v1/snapshot');
    },
    contactActivity: function (contactId) {
      return call('outreach', 'get', '/v1/contacts/' + encodeURIComponent(contactId) + '/activity');
    },
    changesSince: function (cursor) {
      return call('outreach', 'get', '/v1/changes?after=' + encodeURIComponent(cursor || 0));
    }
  };
})();

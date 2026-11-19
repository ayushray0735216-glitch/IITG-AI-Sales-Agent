"""Create a reviewable sender patch from source exported through the editor.

No Google access, credentials, sheet changes, or provider calls. Fails rather
than guessing if the inspected sender's insertion points have changed.
"""
import argparse
import json
from pathlib import Path


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError("The sender changed; inspect its current source before applying this patch.")
    return source.replace(before, after, 1)


def build_patch(reference):
    sender = replace_once(reference["processNextProspect"],
                          "function processNextProspect()", "function processNextProspectLegacy_()")
    marker = "    // ========================================================\n    // MARK GENERATING"
    sender = replace_once(sender, marker,
        "    const sharedDelivery = SharedOutreach.prepare(sheet, rowNumber, headers, data[rowIndex], campaign);\n\n" + marker)
    sender = replace_once(sender, "    MailApp.sendEmail(sendOptions);", """    SharedOutreach.beforeSend(sharedDelivery);
    try {
      MailApp.sendEmail(sendOptions);
    } catch (providerError) {
      SharedOutreach.uncertain(sharedDelivery);
      throw new Error('Provider acceptance is uncertain. Check the sending account before retrying.');
    }
    SharedOutreach.accepted(sharedDelivery);""")
    campaign = replace_once(reference["getCampaignSettings"],
        "function getCampaignSettings()", "function getCampaignSettingsLegacy_()")
    return ("// Reviewed integration replacements for the inspected sender.\n"
            "// Replace the ORIGINAL getCampaignSettings/processNextProspect functions.\n"
            "// Also replace buildEmailHtml/buildPlainTextEmail with these unchanged copies.\n"
            "// Do not leave duplicate definitions. Public entry points are in shared_outreach_hooks.gs.\n\n"
            + campaign + "\n" + sender + "\n" + reference["buildEmailHtml"] + "\n" + reference["buildPlainTextEmail"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    reference = {key: value.replace("\r\n", "\n") for key, value in reference.items()}
    args.output.write_text(build_patch(reference), encoding="utf-8")
    print("Prepared sender replacements and unchanged email builders. No live script was changed.")

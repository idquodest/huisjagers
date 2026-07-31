# Per-source field targeting for the mobile auto-fill injection script
# (see dashboard/app.py's /apply/{listing_id}/auto and notifier.py's ntfy
# "Auto-apply" action button - both need the same list of which sources
# are actually supported). Each source's own contact/apply form has a
# different field name and a different way to reach it from the listing
# page - keyed by source name so a new source is just a new entry here,
# not a code change. Selectors were found by hand (view-source on a real,
# logged-in contact form), not guessed - a source with no entry here
# simply isn't supported yet.
SOURCE_APPLY_INJECTORS = {
    "pararius": {
        # Pararius's own name for the "why do you want this place" field on
        # its /contact/{id} page, reached by clicking "Contact the estate
        # agent"/"Contact the provider" from the listing page itself.
        "motivation_selector": 'textarea[name="contact_agent_huurprofiel_form[motivation]"]',
        "contact_button_text": ["contact the estate agent", "contact the provider"],
    },
}

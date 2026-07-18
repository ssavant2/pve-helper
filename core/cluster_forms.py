from __future__ import annotations

from django import forms

from core.models import cluster_key_validator
from core.services.cluster_trust import TRUST_CA_PEM, TRUST_PUBLIC


class ClusterInspectForm(forms.Form):
    display_name = forms.CharField(max_length=160, label="Display name")
    cluster_key = forms.CharField(
        max_length=63,
        label="Cluster key",
        validators=[cluster_key_validator],
        help_text="Permanent lowercase identity used in URLs; it cannot be renamed later.",
    )
    endpoint_url = forms.URLField(
        max_length=500,
        label="First endpoint URL",
        assume_scheme="https",
        help_text="HTTPS Proxmox API root, normally https://host.example:8006.",
    )
    endpoint_name = forms.RegexField(
        regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$",
        max_length=120,
        required=False,
        label="Endpoint name",
        help_text="Label for this single node/endpoint (not the cluster). Optional; defaults to the first hostname component of the URL.",
    )

    def clean_cluster_key(self):
        return self.cleaned_data["cluster_key"].strip().lower()


class TrustCredentialForm(forms.Form):
    inspection = forms.CharField(widget=forms.HiddenInput)
    trust_mode = forms.ChoiceField(
        choices=(
            (TRUST_PUBLIC, "Publicly trusted certificate"),
            (TRUST_CA_PEM, "Internal CA bundle"),
        ),
        label="Transport trust",
    )
    ca_pem = forms.CharField(
        required=False,
        max_length=65536,
        label="Internal CA bundle (PEM)",
        widget=forms.Textarea(attrs={"rows": 8, "spellcheck": "false"}),
    )
    token_id = forms.CharField(
        max_length=255,
        label="API token ID",
        help_text="Format: user@realm!token-name",
    )
    token_secret = forms.CharField(
        max_length=1024,
        label="API token secret",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    confirm_certificate = forms.BooleanField(
        label="I have reviewed the certificate shown above and approve this transport.",
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("trust_mode") == TRUST_CA_PEM and not str(cleaned.get("ca_pem") or "").strip():
            self.add_error("ca_pem", "Paste the CA certificate bundle used to verify this endpoint.")
        return cleaned


class ClusterConfirmForm(forms.Form):
    candidate = forms.CharField(widget=forms.HiddenInput)
    confirm_identity = forms.BooleanField(
        label="Bind this permanent cluster key to the verified Proxmox CA identity shown above.",
    )


class EndpointInspectForm(forms.Form):
    endpoint_url = forms.URLField(
        max_length=500,
        label="Endpoint URL",
        assume_scheme="https",
    )
    endpoint_name = forms.RegexField(
        regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$",
        max_length=120,
        required=False,
        label="Endpoint name",
    )


class EndpointConfirmForm(forms.Form):
    endpoint = forms.CharField(widget=forms.HiddenInput)
    confirm_identity = forms.BooleanField(
        label="Add this verified endpoint to the cluster named above.",
    )


class EndpointTrustConfirmForm(forms.Form):
    inspection = forms.CharField(widget=forms.HiddenInput)
    confirm_certificate = forms.BooleanField(
        label="I have reviewed this certificate and approve sending the cluster credential to this endpoint.",
    )


class CredentialRotationForm(forms.Form):
    token_id = forms.CharField(max_length=255, label="API token ID")
    token_secret = forms.CharField(
        max_length=1024,
        label="API token secret",
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )


class ClusterDisplayNameForm(forms.Form):
    display_name = forms.CharField(max_length=160, label="Display name")

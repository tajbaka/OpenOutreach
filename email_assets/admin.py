from django import forms
from django.contrib import admin, messages

from email_assets.models import EmailAsset, LeadEmailAsset
from email_assets.services import (
    EmailAssetError,
    approve_asset,
    assign_asset,
    refresh_asset,
    register_asset,
)

# Admin may change only these asset fields. Status, hashes and provider facts
# belong to the locked services (approve/refresh/retire).
EDITABLE_ASSET_FIELDS = ("name", "notes")


class EmailAssetRegistrationForm(forms.ModelForm):
    """Paste a Drive link; the file is fetched and hashed on save."""

    link = forms.CharField(
        label="Google Drive file link",
        help_text="The file stays private: share it with the OpenOutreach service account as a Viewer.",
    )

    class Meta:
        model = EmailAsset
        fields = ("key", "name", "filename", "notes")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["filename"].required = False
        self.fields["filename"].help_text = "Optional; defaults to the Drive file name."
        self.registered_asset = None

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        try:
            asset, _snapshot = register_asset(
                url=cleaned.get("link") or "",
                key=cleaned.get("key") or "",
                name=cleaned.get("name") or "",
                filename=cleaned.get("filename") or "",
                notes=cleaned.get("notes") or "",
                registered_by="admin",  # replaced with the signed-in user on save
                apply=False,
            )
        except EmailAssetError as exc:
            raise forms.ValidationError(str(exc)) from None
        self.registered_asset = asset
        return cleaned

    def _post_clean(self):
        # The provider-derived fields are validated by register_asset in clean().
        pass


@admin.register(EmailAsset)
class EmailAssetAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "status", "filename", "size_bytes", "approved_by", "updated_at")
    list_filter = ("status", "provider")
    search_fields = ("key", "name", "filename", "provider_file_id")
    actions = ("approve_current_bytes", "refresh_from_source")
    readonly_fields = (
        "key", "provider", "provider_file_id", "source_url", "filename", "mime_type",
        "size_bytes", "sha256", "provider_checksum", "provider_revision",
        "provider_modified_at", "status", "registered_by", "approved_by", "approved_at",
        "approved_sha256", "created_at", "updated_at",
    )
    fields = ("name", "notes", *readonly_fields)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = EmailAssetRegistrationForm
            kwargs["fields"] = ("link", "key", "name", "filename", "notes")
        return super().get_form(request, obj, **kwargs)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("link", "key", "name", "filename", "notes")
        return super().get_fields(request, obj)

    def get_readonly_fields(self, request, obj=None):
        return () if obj is None else self.readonly_fields

    def save_model(self, request, obj, form, change):
        if change:
            # Write only what the operator edited. A full save would write the
            # status and hashes loaded with this form back over a concurrent
            # retirement or refresh, making a retired file sendable again.
            fields = [name for name in EDITABLE_ASSET_FIELDS if name in form.changed_data]
            if fields:
                obj.save(update_fields={*fields, "updated_at"})
            return
        asset = form.registered_asset
        for field in asset._meta.concrete_fields:
            if field.primary_key:
                continue
            setattr(obj, field.attname, getattr(asset, field.attname))
        obj.registered_by = request.user.get_username()
        obj.status = EmailAsset.Status.PENDING
        obj.full_clean()
        obj.save()

    @admin.action(description="Approve the current registered bytes")
    def approve_current_bytes(self, request, queryset):
        for asset in queryset:
            try:
                _asset, already = approve_asset(
                    key=asset.key,
                    approved_by=request.user.get_username(),
                    sha256=asset.sha256,
                    apply=True,
                )
            except EmailAssetError as exc:
                self.message_user(request, f"{asset.key}: {exc}", messages.ERROR)
                continue
            self.message_user(
                request,
                f"{asset.key}: {'already approved' if already else 'approved'} "
                f"(SHA-256 {asset.sha256[:12]}…)",
                messages.SUCCESS,
            )

    @admin.action(description="Re-read the file from its source")
    def refresh_from_source(self, request, queryset):
        for asset in queryset:
            try:
                refreshed, changed, _snapshot = refresh_asset(key=asset.key, apply=True)
            except EmailAssetError as exc:
                self.message_user(request, f"{asset.key}: {exc}", messages.ERROR)
                continue
            if changed:
                self.message_user(
                    request,
                    f"{asset.key}: the file changed and now awaits approval "
                    f"(new SHA-256 {refreshed.sha256[:12]}…)",
                    messages.WARNING,
                )
            else:
                self.message_user(request, f"{asset.key}: unchanged", messages.SUCCESS)

    def has_delete_permission(self, request, obj=None):
        # Sent messages reference assets as evidence; retire instead.
        return False


class LeadEmailAssetForm(forms.ModelForm):
    class Meta:
        model = LeadEmailAsset
        fields = ("lead", "slot", "asset")

    def clean_asset(self):
        asset = self.cleaned_data.get("asset")
        choosing = self.instance.pk is None or "asset" in self.changed_data
        if asset is not None and choosing and asset.status == EmailAsset.Status.RETIRED:
            raise forms.ValidationError("This asset is retired; choose another file.")
        return asset


@admin.register(LeadEmailAsset)
class LeadEmailAssetAdmin(admin.ModelAdmin):
    form = LeadEmailAssetForm
    list_display = ("lead", "slot", "asset", "assigned_by", "assigned_at")
    list_filter = ("slot",)
    search_fields = ("lead__first_name", "lead__last_name", "lead__company_name", "asset__key", "slot")
    raw_id_fields = ("lead",)
    autocomplete_fields = ("asset",)

    def get_readonly_fields(self, request, obj=None):
        # An existing row changes only its file; move a slot by removing it.
        if obj is None:
            return ("assigned_by", "assigned_at")
        return ("lead", "slot", "assigned_by", "assigned_at")

    def save_model(self, request, obj, form, change):
        if change and "asset" not in form.changed_data:
            # Nothing the operator edited; never write the loaded file back
            # over a concurrent reassignment.
            return
        # The same locked service as assign_email_asset: Lead -> assignment ->
        # asset lock order, retired re-check under lock, assigned_by/at stamp.
        assign_asset(
            key=obj.asset.key,
            slot=obj.slot,
            lead_ids=[obj.lead_id],
            assigned_by=request.user.get_username(),
            apply=True,
        )
        saved = LeadEmailAsset.objects.get(lead_id=obj.lead_id, slot=obj.slot)
        obj.pk = saved.pk
        obj.assigned_by = saved.assigned_by
        obj.assigned_at = saved.assigned_at
        obj._state.adding = False

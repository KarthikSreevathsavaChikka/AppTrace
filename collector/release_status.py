"""Conservative testing-stage signals; never infer a release track from reviews."""
import re

LABELS={
    'unknown':'Not established',
    'possible_beta':'Possible beta · needs verification',
    'possible_testing':'Possible testing / early access · needs verification',
    'beta_program_available':'Beta program available · build track unknown',
    'confirmed_beta':'Beta · manually verified',
    'confirmed_testing':'Testing / early access · manually verified',
}


def assess(metadata,review=None):
    evidence=[]
    title=str(metadata.get('title') or '')
    version=str(metadata.get('version') or '')
    package=str(metadata.get('appId') or metadata.get('package_id') or '')
    description=str(metadata.get('description') or '')
    beta=re.compile(r'\bbeta\b',re.I)
    testing=re.compile(r'\bearly[ -]access\b|\b(?:alpha|preview)\b',re.I)
    for field,value in [('title',title),('version',version)]:
        if beta.search(value):
            evidence.append(dict(kind='possible_beta',source=field,text=value[:180]))
        elif testing.search(value):
            evidence.append(dict(kind='possible_testing',source=field,text=value[:180]))
    if re.search(r'(?:^|\.)(?:beta|test|testing|alpha)(?:\.|$)',package,re.I):
        evidence.append(dict(kind='possible_beta' if re.search(r'(?:^|\.)beta(?:\.|$)',package,re.I) else 'possible_testing',source='package_id',text=package))
    match=re.search(r'\b(?:currently in beta|in open beta|in closed beta|in early access|currently in testing)\b',description,re.I)
    if match:
        evidence.append(dict(kind='possible_testing',source='developer_description',text=description[max(0,match.start()-40):match.end()+80]))
    status='possible_beta' if any(e['kind']=='possible_beta' for e in evidence) else 'possible_testing' if evidence else 'unknown'
    result=dict(status=status,label=LABELS[status],method='metadata_hints' if evidence else 'no_evidence',evidence=evidence,scope='App metadata only; the downloaded APK release track is not established.')
    if review:
        result.update(status=review['status'],label=LABELS[review['status']],method='manual_review',review=dict(review),scope='Manual assessment of the observed listing/version; other builds may differ.')
        if review['observed_version'] and metadata.get('version') and review['observed_version']!=metadata['version']:
            result.update(status='possible_testing',label='Historical testing evidence · current version unverified',method='historical_review',scope='The recorded evidence refers to a different version. Verify the current listing/build again.')
    return result

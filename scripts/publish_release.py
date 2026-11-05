"""CI-only release from the checked-out reviewed commit; never replace a release/tag."""
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def main():
    repository=os.environ['GH_REPOSITORY']
    if repository!='cuijibo12-afk/goldfish':raise SystemExit('Unexpected target repository')
    commit=os.environ['GH_COMMIT']
    version=json.loads(Path('dsh-plugin/package.json').read_text())['version']
    if version!='0.8.0':raise SystemExit('Review publisher before releasing another version')
    tag='v'+version
    base='https://api.github.com/repos/'+repository
    headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json',
             'X-GitHub-Api-Version':'2022-11-28','User-Agent':'Goldfish-reviewed-source-release'}
    def call(url,method='GET',data=None,binary=False):
        body=data if binary else None if data is None else json.dumps(data).encode()
        request=urllib.request.Request(url,body,{**headers,'Content-Type':'application/octet-stream' if binary else 'application/json'},method=method)
        with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
    for path in ('/git/ref/tags/'+tag,'/releases/tags/'+tag):
        try:call(base+path)
        except urllib.error.HTTPError as error:
            if error.code!=404:raise
        else:raise SystemExit('Existing tag/release will not be overwritten: '+tag)
    archive=Path('dist/Goldfish-Memory-v0.8.0-source.zip')
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    notes=Path('docs/RELEASE_NOTES_v0.8.0.md').read_text(encoding='utf-8')
    notes+='\n\nCommit: `'+commit+'`\n\nSource ZIP SHA256: `'+checksum+'`\n'
    release=call(base+'/releases','POST',{'tag_name':tag,'target_commitish':commit,
        'name':'Goldfish Memory v0.8.0','body':notes,'draft':True,'prerelease':False})
    upload=release['upload_url'].split('{')[0]
    for path in (archive,archive.with_suffix('.zip.sha256')):
        asset=call(upload+'?name='+urllib.parse.quote(path.name),'POST',path.read_bytes(),binary=True)
        if asset['size']!=path.stat().st_size:raise SystemExit('Upload size mismatch; release remains draft')
        digest=asset.get('digest')
        if digest and digest!='sha256:'+hashlib.sha256(path.read_bytes()).hexdigest():
            raise SystemExit('Upload digest mismatch; release remains draft')
    result=call(base+'/releases/'+str(release['id']),'PATCH',{'draft':False})
    print(json.dumps({'release':result['html_url'],'tag':tag,'commit':commit,'sourceSha256':checksum}))


if __name__=='__main__':main()

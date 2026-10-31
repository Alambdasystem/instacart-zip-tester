"""Bounded traversal of visible provider navigation and retailer carousels."""
import hashlib
import deadline

def expand(page,state,snapshot,wait_catalog,observed_location,z,allow,query,maximum=4,limit=500):
    if maximum<=0:return state,{'actions':[],'stop_reason':'disabled','action_limit':0,'item_limit':limit}
    cards={(c.get('url'),c.get('name')):c for c in state['cards']};retailers={r['url']:r for r in state['retailers']}
    actions=[];reason='no_visible_next';groups=page.locator('[role="group"][aria-label="retailer"]')
    n=groups.count();start=int(hashlib.sha256(query.encode()).hexdigest()[:8],16)%max(1,n)
    scopes=[('shelf',groups.nth((start+i)%n)) for i in range(n)]
    nav=page.get_by_role('navigation')
    scopes.extend(('navigation',nav.nth(i)) for i in range(nav.count()))
    for kind,scope in scopes:
        if len(actions)>=maximum:reason='action_limit';break
        if len(cards)>=limit:reason='item_limit';break
        if deadline.remaining()<8:reason='time_budget';break
        next_button=scope.get_by_role('button',name='Next page',exact=True)
        if next_button.count()!=1:continue
        button=next_button.first
        if not button.is_visible() or not button.is_enabled():continue
        deadline.check();before=len(cards)
        try:
            button.click(timeout=deadline.bounded_ms(3000))
            current,_=wait_catalog(page,timeout=min(4,max(.1,deadline.remaining()-3)))
        except (deadline.DeadlineExceeded,):raise
        except Exception as error:
            from connector import ConnectorError
            if isinstance(error,ConnectorError):raise
            # Reclassify any newly displayed auth/screening dialog before retaining
            # observations from the previous visible page.
            wait_catalog(page,timeout=.1)
            reason='navigation_failed';break
        observed=observed_location(page)
        if observed!=z and not allow:
            from connector import ConnectorError
            raise ConnectorError('location_required','Provider context changed during visible pagination.')
        for c in current['cards']:cards[(c.get('url'),c.get('name'))]=c
        for r in current['retailers']:retailers[r['url']]=r
        actions.append({'kind':kind,'new_cards':len(cards)-before})
        state=current
    return {**state,'cards':list(cards.values())[:limit],'retailers':list(retailers.values())}, {'actions':actions,'stop_reason':reason,'action_limit':maximum,'item_limit':limit}

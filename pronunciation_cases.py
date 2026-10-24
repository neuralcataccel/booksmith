"""Original context contrasts for ambiguous English readings, grouped by word."""
from jev_cases import case

# Candidate phonemes are engine-specific English-US inputs, not an IPA editor API.
WORDS = [
    ('lead','metal, pronounced led','guide, pronounced leed','lˈɛd','lˈiːd',
     'a lead pipe','lead the way','The shield was made of lead.','The guide will lead us home.'),
    ('read','completed past action, pronounced red','present or future action, pronounced reed','ɹˈɛd','ɹˈiːd',
     'read yesterday','will read','Yesterday, she read the letter.','Tomorrow, she will read the letter.'),
    ('wind','moving air','turn or coil something','wˈɪnd','wˈaɪnd',
     'strong wind','wind the clock','A cold wind blew across the field.','Please wind the clock before supper.'),
    ('tear','liquid from an eye','rip something','tˈɪɹ','tˈɛɹ',
     'a tear in her eye','tear the paper','A single tear rolled down her cheek.','Take care not to tear the thin paper.'),
    ('bow','bend at the waist','archery weapon','bˈaʊ','bˈoʊ',
     'bow to the queen','a bow and arrow','The courtier made a deep bow to the queen.','The hunter pulled back the string of his bow.'),
    ('bass','fish, rhymes with mass','low musical instrument or range, rhymes with base','bˈæs','bˈeɪs',
     'a bass in the lake','a bass guitar','The bass swam under the boat.','She played the bass in the orchestra.'),
    ('close','near or not far away','shut something','klˈoʊs','klˈoʊz',
     'close to home','close the door','The cottage was close to the river.','Please close the window before you leave.'),
    ('live','reside somewhere','happening in real time or alive, rhymes with five','lˈɪv','lˈaɪv',
     'live in a cottage','a live broadcast','They live in a small cottage.','The live broadcast began at noon.'),
    ('dove','bird, rhymes with love','past tense of dive, rhymes with drove','dˈʌv','dˈoʊv',
     'a white dove','dove into the pool','A white dove perched on the wall.','The swimmer dove into the pool.'),
    ('wound','injury','past tense of wind or wrap, rhymes with found','wˈuːnd','wˈaʊnd',
     'a painful wound','wound the thread','The doctor cleaned the painful wound.','She wound the thread around the spool.'),
    ('sow','plant seeds, rhymes with go','female pig, rhymes with cow','sˈoʊ','sˈaʊ',
     'sow the seeds','a sow and her piglets','The farmer will sow the seeds tomorrow.','The sow rested beside her newborn piglets.'),
    ('does','form of the verb do, rhymes with buzz','plural of female deer, rhymes with goes','dˈʌz','dˈoʊz',
     'does the work','two does in the forest','He does the work without complaint.','Two does grazed beside the forest.')]


def cases():
    rows=[]
    for i,(word,a,b,pa,pb,phrase_a,phrase_b,sentence_a,sentence_b) in enumerate(WORDS):
        choices={'a':a,'b':b,'unknown':'The provided context does not establish the intended sense or reading'}
        split='development' if i<5 else 'holdout'
        for label,phrase,sentence in [('a',phrase_a,sentence_a),('b',phrase_b,sentence_b)]:
            paragraph='The narrator describes the following action or object. '+sentence+' Everyone then continues with the same activity.'
            scene='The scene is told in ordinary English, with no puns, quoted pronunciation rules or unusual dialect. '+paragraph+' The characters move on to a different topic.'
            row=case('pronunciation',word+'-'+label,word,sentence,paragraph,scene,label,
                     token_expected='unknown',choices=choices,split=split)
            row['contexts']={'token':word,'phrase':phrase,**{k:v for k,v in row['contexts'].items() if k!='token'}}
            row['expected']['phrase']=label;row['group']='word-'+word
            row.update(word=word,candidate_phonemes={'a':pa,'b':pb})
            rows.append(row)
    # Here the target paragraph really lacks the cue; the previous scene supplies it.
    for word,token,sentence,context,label in [
        ('read','read','I read the letter.','Yesterday I arrived at my desk and picked up the envelope. Then I performed the target action.','a'),
        ('bass','bass','She examined the bass.','She was an expert in freshwater fish; the entire scene takes place at a fish hatchery.','a'),
        ('bow','bow','He admired the bow.','He was shopping for an archery weapon, and there were no ships, musical instruments or ceremonial gestures in this scene.','b'),
        ('wound','wound','She pointed to the wound.','A doctor had just treated her arm for an injury. The bandage covered the damaged skin.','a')]:
        info=next(x for x in WORDS if x[0]==word);choices={'a':info[1],'b':info[2],'unknown':'Not established'}
        row=case('pronunciation',word+'-late-cue',token,sentence,sentence,context+'\nTarget paragraph: '+sentence,label,
                 sentence_expected='unknown',choices=choices,split='development' if word in {'read','bow'} else 'holdout')
        row['contexts']={'token':token,'phrase':sentence,**{k:v for k,v in row['contexts'].items() if k!='token'}}
        row['expected']['phrase']='unknown';row['expected']['paragraph']='unknown';row['group']='word-'+word
        row.update(word=word,candidate_phonemes={'a':info[3],'b':info[4]});rows.append(row)
    return rows


def final_holdout():
    """New word groups, reserved for one frozen marked-target/gating check."""
    contrasts=[
        ('record','noun, a saved account; stress first syllable','verb, capture an account; stress second syllable','She kept a written record of the visit.','Please record the birdsong.'),
        ('present','noun, a gift; stress first syllable','verb, offer or show; stress second syllable','The present was wrapped in blue paper.','She will present the award.'),
        ('object','noun, a thing; stress first syllable','verb, oppose; stress second syllable','A round object lay on the table.','They object to the proposal.'),
        ('refuse','noun, garbage; stress first syllable','verb, decline; stress second syllable','The refuse was collected each Monday.','She will refuse the offer.'),
        ('produce','noun, fruit and vegetables; stress first syllable','verb, create or grow; stress second syllable','They bought fresh produce at the market.','The farmers produce vegetables.'),
        ('permit','noun, an authorization document; stress first syllable','verb, allow; stress second syllable','A driver\'s permit was needed.','They will permit the child to go.'),
        ('conduct','noun, behavior; stress first syllable','verb, lead an orchestra; stress second syllable','His conduct was polite.','The teacher will conduct the orchestra.'),
        ('content','noun, subject matter; stress first syllable','adjective, satisfied; stress second syllable','The content of the letter surprised her.','She was content to remain at home.'),
        ('entrance','noun, an entryway; stress first syllable','verb, captivate; stress second syllable','The entrance to the cave was narrow.','The music will entrance every listener.'),
        ('sewer','noun, a drain, pronounced soo-er','noun, a person who sews, pronounced soh-er','The brick sewer ran under the street.','The expert sewer finished the hem.')]
    rows=[]
    def add(word,a,b,text,label,name):
        row=case('pronunciation','final-'+word+'-'+name,word,text,text,text,label,
                 choices={'a':a,'b':b,'unknown':'The target reading is not determined by this context'},split='holdout')
        row.update(word=word,group='final-word-'+word,contexts={'paragraph':text},expected={'paragraph':label})
        rows.append(row)
    for word,a,b,sa,sb in contrasts:
        add(word,a,b,sa,'a','a');add(word,a,b,sb,'b','b')
    for word,a,b,_,_ in contrasts[:4]:add(word,a,b,word,'unknown','missing-context')
    for word,a,b,context,label in [
        ('sewer',contrasts[-1][1],contrasts[-1][2],'The sewing circle watched her work with a needle and thread. The sewer was nearby.','b'),
        ('sewer',contrasts[-1][1],contrasts[-1][2],'The city crew inspected underground drainage. The sewer was nearby.','a'),
        ('entrance',contrasts[-2][1],contrasts[-2][2],'The line on the card was only the word entrance. No sentence or use was supplied.','unknown'),
        ('content',contrasts[-3][1],contrasts[-3][2],'The line on the card was only the word content. No sentence or use was supplied.','unknown')]:
        add(word,a,b,context,label,'late-'+str(len(rows)))
    return rows

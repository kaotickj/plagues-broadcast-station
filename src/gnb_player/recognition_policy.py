"""Shared audio-only recognition contract; no model or GUI imports."""


def audio_only_policy(request):
    """Fail explicitly rather than silently accepting a vocabulary/prompt option.

    Track metadata and reviewed vocabulary remain review evidence. They are not
    recognition inputs. Supplied text belongs only to the separate align mode.
    """
    for values in (request, request.get('config', {})):
        for key in ('context', 'hotwords', 'vocabulary', 'vocabulary_context',
                    'initial_prompt', 'prompt', 'prompt_ids', 'decoder_input_ids',
                    'automatic_vocabulary_injection'):
            if values.get(key):
                raise ValueError('Automatic vocabulary injection is disabled. '
                                 'Review name suggestions separately; no model job started.')
    return {'automatic_vocabulary_injection': False, 'context': '',
            'track_title_used_as_prompt': False, 'policy': 'audio_only_v1'}

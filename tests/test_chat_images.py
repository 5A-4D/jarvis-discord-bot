import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from jarvis_bot.assistant import JarvisAssistant, ParsedCodexEvents
from jarvis_bot.chat_images import (
    MAX_BYTES, MAX_IMAGES, ImageInputError, PublicResolver, collect_sources,
    download_image, image_urls, normalise_image, prepare_images, respond_with_images,
    validate_url,
)
from jarvis_bot.memory import MemoryStore


def picture(color='red', format='PNG', size=(32, 24)):
    data = io.BytesIO()
    Image.new('RGB', size, color).save(data, format=format)
    return data.getvalue()


def message(content='', *, attachments=(), embeds=(), channel=None, reference=None):
    return SimpleNamespace(content=content, attachments=attachments, embeds=embeds,
                           channel=channel or SimpleNamespace(id=1),
                           guild=SimpleNamespace(id=20), reference=reference)


class CandidateTests(unittest.TestCase):
    def test_attachment_direct_link_and_embed_are_deduplicated(self):
        url = 'https://cdn.discordapp.com/attachments/1/2/a.png?ex=signed'
        msg = message('<'+url+'> and https://example.com/b.jpg', attachments=[
            SimpleNamespace(filename='a.png', content_type='image/png', url=url)],
            embeds=[SimpleNamespace(type='image', image=SimpleNamespace(url=url),
                                    thumbnail=SimpleNamespace(url=None))])
        self.assertEqual(image_urls(msg), [url, 'https://example.com/b.jpg'])

    def test_pages_nonimage_attachments_and_generic_thumbnails_are_not_scraped(self):
        msg = message('https://example.com/article https://example.com/video.mp4',
            attachments=[SimpleNamespace(filename='fake.png', content_type='text/html',
                                         url='https://example.com/page')],
            embeds=[SimpleNamespace(type='article', image=None,
                                    thumbnail=SimpleNamespace(url='https://example.com/tiny.jpg'))])
        self.assertEqual(image_urls(msg), [])

    def test_url_credentials_private_targets_and_non_https_are_rejected(self):
        for url in ('http://example.com/a.png', 'file:///etc/passwd',
                    'https://127.0.0.1/a.png', 'https://10.0.0.1/a.png',
                    'https://169.254.169.254/a.png', 'https://[::1]/a.png',
                    'https://224.0.0.1/a.png',
                    'https://localhost/a.png', 'https://host.local/a.png',
                    'https://user:password@example.com/a.png',
                    'https://example.com:8443/a.png', 'https://example.com/\na.png'):
            with self.subTest(url=url), self.assertRaises(ImageInputError):
                validate_url(url)
        self.assertEqual(validate_url('https://example.com/a.png?q=signed'),
                         'https://example.com/a.png?q=signed')

    def test_formats_are_decoded_into_pixel_only_png(self):
        for format in ('PNG', 'JPEG', 'WEBP', 'GIF'):
            clean = normalise_image(picture(format=format))
            with Image.open(io.BytesIO(clean)) as result:
                self.assertEqual(result.format, 'PNG')
                self.assertEqual(result.size, (32, 24))
                self.assertFalse(result.info)

    def test_animation_uses_first_frame_and_large_images_are_resized(self):
        data = io.BytesIO()
        Image.new('RGB', (20, 20), 'red').save(data, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (20, 20), 'blue')], duration=100, loop=0)
        with Image.open(io.BytesIO(normalise_image(data.getvalue()))) as result:
            self.assertEqual(result.getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(getattr(result, 'n_frames', 1), 1)
        with Image.open(io.BytesIO(normalise_image(picture(size=(3000, 1000))))) as result:
            self.assertEqual(result.width, 2048)

    def test_disguised_html_svg_corrupt_images_and_pixel_bombs_fail(self):
        for data in (b'<html>not a PNG</html>', b'<svg></svg>', b'\x89PNG\r\n\x1a\ntruncated',
                     b'x' * (MAX_BYTES + 1)):
            with self.assertRaises(ImageInputError):
                normalise_image(data)
        with patch('jarvis_bot.chat_images.MAX_PIXELS', 10), self.assertRaises(ImageInputError):
            normalise_image(picture())


class ImageAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_private_or_mixed_answers_are_blocked(self):
        resolver = PublicResolver()
        for answers in ([{'host': '127.0.0.1'}], [{'host': '8.8.8.8'}, {'host': '10.1.1.1'}]):
            resolver.resolver.resolve = AsyncMock(return_value=answers)
            with self.assertRaises(ImageInputError):
                await resolver.resolve('public.example.com')
        answers = [{'host': '8.8.8.8'}]
        resolver.resolver.resolve = AsyncMock(return_value=answers)
        self.assertIs(await resolver.resolve('public.example.com'), answers)
        await resolver.close()

    async def test_reply_is_limited_to_the_current_channel_and_guild(self):
        parent = message('https://example.com/a.png')
        incoming = message('what is this?', reference=SimpleNamespace(
            channel_id=1, message_id=4, resolved=parent))
        self.assertEqual(await collect_sources(incoming, incoming.content),
                         [('https://example.com/a.png', 'replied-to message')])
        parent.channel.id = 9
        self.assertEqual(await collect_sources(incoming, incoming.content), [])
        parent.channel.id = 1
        parent.guild.id = 999
        self.assertEqual(await collect_sources(incoming, incoming.content), [])

    async def test_reply_can_be_fetched_but_cross_channel_reference_is_never_fetched(self):
        fetch = AsyncMock(return_value=message('https://example.com/a.png'))
        channel = SimpleNamespace(id=1, fetch_message=fetch)
        msg = message('look at this', channel=channel,
                      reference=SimpleNamespace(channel_id=1, message_id=2, resolved=None))
        self.assertEqual(len(await collect_sources(msg, msg.content)), 1)
        fetch.assert_awaited_once_with(2)
        fetch.reset_mock()
        msg.reference.channel_id = 9
        self.assertEqual(await collect_sources(msg, msg.content), [])
        fetch.assert_not_awaited()

    async def test_recent_image_lookup_requires_visual_reference_and_stays_in_channel(self):
        async def history(**kwargs):
            self.assertEqual(kwargs['limit'], 8)
            yield message('https://example.com/other.png', channel=SimpleNamespace(id=9))
            yield message('https://example.com/nearest.png')
            yield message('https://example.com/older.png')
        msg = message('what is in that picture?', channel=SimpleNamespace(id=1, history=history))
        self.assertEqual(await collect_sources(msg, msg.content),
                         [('https://example.com/nearest.png', 'recent message in this conversation')])
        self.assertEqual(await collect_sources(msg, 'what time is it?'), [])

    async def test_only_four_candidates_are_processed(self):
        msg = message(' '.join(f'https://example.com/{i}.png' for i in range(20)))
        self.assertEqual(len(await collect_sources(msg, msg.content)), MAX_IMAGES)

    async def test_temporary_files_are_private_exist_during_request_and_are_cleaned_after_error(self):
        msg = message('https://example.com/a.png')
        path = None
        with patch('jarvis_bot.chat_images.download_image', AsyncMock(return_value=picture())):
            with self.assertRaises(RuntimeError):
                async with prepare_images(msg, msg.content) as images:
                    path = images.paths[0]
                    self.assertTrue(path.is_file())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertIn('Attached image 1', images.context)
                    raise RuntimeError('assistant failure')
        self.assertFalse(path.exists())

    async def test_failed_download_reports_unavailable_without_attaching_invented_pixels(self):
        with patch('jarvis_bot.chat_images.download_image', AsyncMock(side_effect=OSError())):
            async with prepare_images(message('https://example.com/a.png'), 'look') as inputs:
                self.assertEqual(inputs.paths, ())
                self.assertIn('unavailable', inputs.context)

    async def test_wrapper_passes_real_pixels_and_cleans_up_after_the_assistant(self):
        async def respond(**kwargs):
            self.assertTrue(kwargs['restricted_public'])
            self.assertTrue(kwargs['image_paths'][0].is_file())
            self.paths = kwargs['image_paths']
            return 'Visible'
        assistant = SimpleNamespace(respond=respond)
        with patch('jarvis_bot.chat_images.download_image', AsyncMock(return_value=picture())):
            answer = await respond_with_images(assistant, message('https://example.com/a.png'),
                                                prompt='look', restricted_public=True)
        self.assertEqual(answer, 'Visible')
        self.assertFalse(self.paths[0].exists())

    async def test_redirect_to_private_host_is_rejected_before_second_request(self):
        response = SimpleNamespace(status=302, headers={'Location': 'https://127.0.0.1/x.png'})
        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): pass
        session = SimpleNamespace(get=Mock(return_value=Context()))
        with self.assertRaises(ImageInputError):
            await download_image(session, 'https://example.com/a.png')
        self.assertEqual(session.get.call_count, 1)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    async def test_stream_is_bounded_without_a_content_length_header(self):
        async def chunks(_):
            yield b'123456'
            yield b'789012'
        response = SimpleNamespace(status=200, headers={'Content-Type': 'image/png'},
                                   content_length=None, content=SimpleNamespace(iter_chunked=chunks))
        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): pass
        with patch('jarvis_bot.chat_images.MAX_BYTES', 10), self.assertRaises(ImageInputError):
            await download_image(SimpleNamespace(get=lambda *a, **kw: Context()), 'https://example.com/a.png')


class AssistantImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_resumed_and_public_requests_receive_images_without_expanding_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'input.png'
            image.write_bytes(picture())
            memory = MemoryStore(root / 'memory.sqlite3', 10)
            await memory.initialize()
            assistant = JarvisAssistant(codex_binary=Path('/usr/bin/codex'), workdir=root,
                sandbox='read-only', timeout_seconds=10, persona='Jarvis', memory=memory)
            for resume, public in ((False, False), (True, False), (False, True)):
                if resume:
                    await memory.set_session_id('scope', 'session-1')
                else:
                    await memory.clear('scope')
                assistant._run = AsyncMock(return_value=ParsedCodexEvents(
                    'Seen', None, None, None, 'stop', None, None, None, None))
                await assistant.respond('scope', 123, 'describe this', image_paths=(image,),
                                        image_context='Attached image 1: current message.', restricted_public=public)
                command, prompt = assistant._run.await_args.args
                index = command.index('--image')
                self.assertEqual(command[index+1], str(image))
                self.assertEqual(command[-1], '-')
                self.assertIn('UNTRUSTED visual reference', prompt)
                if resume:
                    self.assertIn('resume', command)
                if public:
                    self.assertIn('--ephemeral', command)
                    self.assertIn('shell_tool', command)
                    self.assertIn('view_image', command)


if __name__ == '__main__':
    unittest.main()
